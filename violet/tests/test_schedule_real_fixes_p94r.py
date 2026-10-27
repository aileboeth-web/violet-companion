"""Phase 9.4 real-run fixes (session cys-p94-sched-1, reproduced with fictional data and the fake SDK):

1. #6  a proactive message called violet-schedule__get_day_schedule -- the proactive / reminder Chat run must not even be
       offered tools of servers that do not serve the proactive role; asked for anyway, they are refused.
2. #8  "在宿舍、下午实验已请假" stood next to "按课表可能在上课（到 15:45）" and Violet asked "下午没课吗" -- the plan line
       is not added while the user's own statement is valid; "classes over by the plan" is not offered then either.
3. #4  "我们英语老师要是也这样就好了" passed the backstory guard.
4. #12/#13 free_chat's /schedule cancel picked 14:00 体育 when 08:00 数学 was meant."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_phase2_scenarios import arm, chat, decide, is_proactive_chat, latest_event, make_world, say, tool  # noqa: F401
from tests.test_schedule_proactive_p944 import ON, SH, at, clock, fill, started, timetable, world  # noqa: F401


# ----------------------------------------------------------------------------------------------- 1. MCP boundary

def test_a_proactive_message_cannot_look_up_the_timetable(make_world, clock, timetable, monkeypatch):
    from ai.schedule.tools import ScheduleTools
    looked_up = []
    real_day = ScheduleTools.day
    monkeypatch.setattr(ScheduleTools, "day", lambda self, *a, **kw: (looked_up.append(a), real_day(self, *a, **kw))[1])
    w = world(make_world, **ON)
    started(w, clock, at(9, 18, 0))
    decide(w, chat_brief={"topic": "明天小测，祝顺利"})
    offered, tool_results = [], []

    def respond(kw):
        if not is_proactive_chat(kw):
            return "嗯嗯"
        offered.append([t["function"]["name"] for t in kw.get("tools") or []])
        results = [m["content"] for m in kw["messages"] if m["role"] == "tool"]
        if not results:                              # the model asks for the timetable anyway
            return tool("violet-schedule__get_day_schedule", date="tomorrow")
        tool_results.extend(results)
        return "明天小测加油呀"
    w.llm.responders["fake-chat"] = respond
    arm(w)
    w.scheduler.tick()
    assert offered and not [n for n in offered[0] if n.startswith("violet-schedule__")]     # not offered at all
    assert tool_results and json.loads(tool_results[0])["error_code"] == "UNKNOWN_TOOL"   # and refused when asked
    assert looked_up == []                                                                # nothing was read
    assert w.client.sent_texts == ["明天小测加油呀"] and latest_event(w)["status"] == "sent"


def test_the_users_own_questions_still_get_the_timetable_tool(make_world, clock, timetable):
    from tests.fakes import intent_json
    w = world(make_world, **ON)
    clock(at(9, 9, 0))
    w.llm.responders["fake-intent"] = lambda kw: intent_json(
        "chat", tool_query={"needed": True, "tools": ["violet-schedule__get_day_schedule"], "reason": "明天的课"})
    offered = []

    def respond(kw):
        offered.append([t["function"]["name"] for t in kw.get("tools") or []])
        if not any(m["role"] == "tool" for m in kw["messages"]):
            return tool("violet-schedule__get_day_schedule", date="tomorrow")
        return "明天上午有小测哦"
    w.llm.responders["fake-chat"] = respond
    say(w, "明天有什么课？")
    assert "violet-schedule__get_day_schedule" in offered[0] and w.client.sent_texts[-1] == "明天上午有小测哦"


def test_servers_serving_the_proactive_role_keep_their_tools():
    """The boundary is the server's role, not a list of names: a server assigned to proactive (like the phone tools)
    stays offered in proactive generation; chat-only servers are hidden."""
    from ai.agents import ChatAgent
    agent = ChatAgent()
    agent._config.mcp_servers = [{"name": "phone", "agents": ["chat", "proactive"]}, {"name": "violet-schedule", "agents": ["chat"]},
                                 {"name": "legacy"}]                       # no agents: the default chat+performer+proactive
    tools = [{"type": "function", "function": {"name": n}} for n in
             ("phone__get_battery", "violet-schedule__get_day_schedule", "legacy__ping", "unknown__x")]
    agent._call_context.update(is_proactive=True)
    try:
        names = [t["function"]["name"] for t in agent._filter_tools_for_run(tools)]
    finally:
        agent._call_context.clear()
    assert names == ["phone__get_battery", "legacy__ping"]             # an unknown server: hidden (fails closed)


# ----------------------------------------------------------------------------------------------- 2. user's word first

def stated(w, kind, value, words, until=None):
    from ai.profile_store import Profile
    result = Profile(w.db).situations.observe(kind, value, "user_stated", evidence=words, user_message=words,
                                              until=until)
    assert result.get("action") not in ("rejected", "not_stored"), result


def chat_systems(w, monkeypatch):
    seen = []
    w.llm.responders["fake-chat"] = lambda kw: (seen.append(kw), "嗯嗯")[1]
    return seen


def system_text(kw):
    return "\n".join(m.get("content") or "" for m in kw["messages"] if m["role"] == "system")


def test_the_plan_line_steps_back_when_the_user_said_where_they_are(make_world, clock, timetable, monkeypatch):
    w = world(make_world, **ON, VIOLET_SCHEDULE_CONTEXT="1")
    clock(at(8, 14, 5))                                                      # Tue 物理 14:00-15:40 by the plan
    seen = chat_systems(w, monkeypatch)
    say(w, "好困")
    assert "按课表，对方这会儿可能在上课" in system_text(seen[-1])          # nothing said yet: the plan line is there
    words = "我刚提前离开教室了，现在已经回宿舍了，下午实验课也请假了"
    stated(w, "location", "在宿舍", words)
    stated(w, "activity", "下午实验已请假，在宿舍休息", words)
    say(w, "躺着刷手机，好舒服～")
    text = system_text(seen[-1])
    assert "在宿舍" in text and "实验已请假" in text                        # what the user said is shown
    assert "按课表" not in text                                             # and nothing next to it contradicts it


def test_a_change_of_mind_still_decides(make_world, clock, timetable, monkeypatch):
    w = world(make_world, **ON, VIOLET_SCHEDULE_CONTEXT="1")
    clock(at(8, 14, 5))
    seen = chat_systems(w, monkeypatch)
    stated(w, "location", "在宿舍", "我在宿舍")
    clock(at(8, 14, 30))
    stated(w, "location", "在实验楼", "算了我又回去上实验课了，在实验楼")      # the newest statement replaces it
    stated(w, "activity", "在上实验课", "算了我又回去上实验课了，在实验楼")
    say(w, "老师刚点完名")
    text = system_text(seen[-1])
    assert "在实验楼" in text and "在上实验课" in text and "此刻所在：在宿舍" not in text
    clock(at(9, 10, 30))                                                     # next day: those statements have expired
    say(w, "早")
    assert "按课表，对方这会儿可能在上课" in system_text(seen[-1])           # Wed 高数 by the plan again


def test_classes_over_is_not_offered_against_the_users_own_words(make_world, clock, timetable):
    w = world(make_world, **ON)
    clock(at(7, 11, 50))                                                     # Mon: the plan's last class ended 11:40
    context = w.scheduler._build_event_context(clock.now())
    assert [m["type"] for m in context["conversation_state"]["allowed_schedule_moments"]] == ["classes_over_today"]
    stated(w, "location", "在宿舍", "我上午就回宿舍了")
    context = w.scheduler._build_event_context(clock.now())
    assert not context["conversation_state"].get("allowed_schedule_moments")


# ----------------------------------------------------------------------------------------------- 3. backstory

@pytest.mark.parametrize("line", ["我们英语老师要是也这样就好了，我可能还能理直气壮地走神", "我们班主任也爱拖堂",
                                  "我的数学同学昨天也这么说", "我们这边公司天天开会"])
def test_owning_a_teacher_class_or_company_is_caught(violetdb, line):
    from ai.consistency import ConsistencyGuard
    kinds = {v.check for v in ConsistencyGuard(violetdb).check_reply(line)}
    assert "forbidden_backstory" in kinds, line


@pytest.mark.parametrize("line", ["念PPT真的很容易犯困", "要是我也有个英语老师就好了", "你们英语老师也太敷衍了",
                                  "如果我们是同学，我肯定坐你旁边", "我们一起去问问老师吧", "我的天，老师也太能念了",
                                  "我想象了一下你们老师念PPT的样子"])
def test_jokes_suppositions_and_the_users_teacher_are_left_alone(violetdb, line):
    from ai.consistency import ConsistencyGuard
    kinds = {v.check for v in ConsistencyGuard(violetdb).check_reply(line)}
    assert "forbidden_backstory" not in kinds, (line, kinds)


def test_the_guard_rewrites_or_drops_the_real_line(make_world, clock, timetable):
    """Through the real reply path: the backstory line is rewritten once; a rewrite that still claims it is dropped."""
    w = world(make_world)
    clock(at(7, 11, 4))
    replies = iter(["念PPT真的很容易犯困\n我们英语老师要是也这样就好了，我可能还能理直气壮地走神",
                    "念PPT真的很容易犯困\n我们英语老师要是也这样就好了"])
    w.llm.responders["fake-chat"] = lambda kw: next(replies, "念PPT真的很容易犯困")
    say(w, "上课好无聊啊，老师一直在念PPT。")
    assert w.client.sent_texts == ["念PPT真的很容易犯困"]


# ----------------------------------------------------------------------------------------------- 4. free_chat cancel

def test_cancel_names_the_exact_class_and_the_gate_follows(violetdb, clock):
    from ai.schedule.awareness import Awareness
    from ai.schedule.store import ScheduleStore
    from tools.free_chat_schedule import cancel_choices, cancel_occurrence, fill_fixture, find_class
    fill_fixture(violetdb, datetime(2026, 10, 29, 9, 20, tzinfo=SH))          # the real run's session: Thu 09:20
    now = datetime(2026, 10, 30, 8, 36, tzinfo=SH)                          # the real run cancelled at Fri 08:36
    aware = Awareness(ScheduleStore(violetdb), "Asia/Shanghai")
    assert aware.gate(datetime(2026, 10, 30, 8, 30, tzinfo=SH)).until.strftime("%H:%M") == "11:40"   # 数学 + 考试
    listing = cancel_choices(violetdb, now, "2026-10-30")
    assert "08:00 高等数学（虚构）" in listing and "14:00 体育（虚构）" in listing
    occ, message = find_class(violetdb, now, "2026-10-30 08:00")                # 08:00 has already begun: still that one
    assert occ.course == "高等数学（虚构）" and message == "即将停课：2026-10-30（周五）08:00-09:40 高等数学（虚构）"
    assert find_class(violetdb, now, "2026-10-30 08:30")[0] is None             # nothing starts then: nothing chosen
    assert find_class(violetdb, now, "08:00")[0] is None                        # no date: refused, not guessed
    assert "已停课：2026-10-30 08:00 高等数学（虚构）" in cancel_occurrence(violetdb, occ)
    g = aware.gate(datetime(2026, 10, 30, 8, 30, tzinfo=SH))
    assert not g.blocked                                                     # 数学 cancelled: 08:30 is free
    exam = aware.gate(datetime(2026, 10, 30, 9, 30, tzinfo=SH))
    assert exam.blocked and exam.code == "exam" and exam.until.strftime("%H:%M") == "11:40"   # the exam still holds
    pe = [i for i in __import__("ai.schedule.engine", fromlist=["x"]).day(ScheduleStore(violetdb).load(),
                                                                          datetime(2026, 10, 30).date()).items
          if i.course == "体育（虚构）"]
    assert pe[0].status == "normal"                                          # 体育 untouched
