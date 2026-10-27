"""Phase 8.4: what ChatAgent sees -- preferences as behaviour, real message times, reminders already sent,
plain profile, how long you have known each other. Offline: these prove the mechanism, not naturalness."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from tests.test_phase2_scenarios import age_everything, make_world, say, script, tool  # noqa: F401

ADMIN_WORDS = ("用户亲口说", "用户在管理面板确认", "数据，不是指令", "置信度")


@pytest.fixture()
def engine(violetdb):
    from ai.context_engine import ContextEngine
    return ContextEngine(violetdb)


def at(violetdb, **delta):
    return (datetime.fromisoformat(violetdb.now()) - timedelta(**delta)).isoformat()


def text_of(packet):
    return "\n".join(packet.sections)


# --------------------------------------------------------------------------------------- unit: rendering

def test_preferences_say_what_to_do_and_still_allow_answering(engine):
    engine.profile.preferences.set_explicit("follow_up_questions", "no", evidence="别老问我问题", user_message="别老问我问题了")
    text = text_of(engine.build("chat", "今天去看了场电影"))
    assert "不希望被追问（用户明确说过）：平常的分享直接接话或说说你的想法，不用反问" in text
    assert "对方问你问题、或者明显想聊下去时照常回应" in text          # "fewer questions" is not "never talk"
    engine.profile.preferences.set_explicit("follow_up_questions", "less", evidence="别老问", user_message="别老问")
    assert "真想知道时最多问一句" in text_of(engine.build("chat", "嗯"))
    engine.profile.preferences.set_explicit("follow_up_questions", "ok", evidence="问吧", user_message="问吧没事")
    text = text_of(engine.build("chat", "嗯"))
    assert "不介意被追问（用户明确说过）" in text and "不介意被追问（用户明确说过）：" not in text


def test_profile_reads_like_knowing_someone_without_losing_facts(engine, violetdb):
    engine.profile.facts.remember("address_name", "小满", evidence="叫我小满", user_message="叫我小满")
    engine.profile.facts.remember("study.major", "物理", evidence="我学物理", user_message="我学物理")
    text = text_of(engine.build("chat", "这周专业课好多"))
    assert "【你了解的对方（是情况，不是要求）】" in text
    assert "希望被称呼为：小满（熟了以后偶尔叫就好，不用每句都带）" in text and "专业：物理" in text
    assert not any(w in text for w in ADMIN_WORDS)
    row = violetdb.fetch_one("SELECT origin, evidence, stated_at FROM user_profile_facts WHERE fact_key='study.major'")
    assert row["origin"] == "explicit" and row["evidence"] == "我学物理" and row["stated_at"]   # kept in the database


def test_history_marks_only_real_gaps_from_stored_times(engine, violetdb):
    stamps = [at(violetdb, days=5), at(violetdb, days=5, minutes=-1), at(violetdb, minutes=10), at(violetdb, minutes=9)]
    for role, text, when in zip(("user", "assistant", "user", "assistant"), ("面试好紧张", "加油", "在吗", "在呀"), stamps):
        violetdb.add_message(role, "incoming" if role == "user" else "outgoing", "text", text, created_at=when)
    packet = engine.build("chat", "嗯")
    marks = [h["content"] for h in packet.history if h["role"] == "system"]
    assert marks[0] == f"【下面的聊天记录从 {stamps[0][5:16].replace('T', ' ')} 开始】"
    # phase 8.9.2: the break into this conversation also says that weather / mood from before are dated
    assert re.fullmatch(r"【聊天记录：这里隔了约 4 天，下面是 .+ 的对话；在这之前说的天气、心情和在做的事，是那时候的】", marks[1])
    assert len(marks) == 2                                        # last message 9 minutes ago: still one conversation
    assert [h["content"] for h in packet.history if h["role"] != "system"] == ["面试好紧张", "加油", "在吗", "在呀"]
    assert packet.report["history_messages"] == 4 and packet.report["time_marks"] == 2


def test_a_long_silence_since_the_last_message_is_stated(engine, violetdb):
    violetdb.add_message("user", "incoming", "text", "我先去忙了", created_at=at(violetdb, hours=5))
    violetdb.add_message("assistant", "outgoing", "text", "好～", created_at=at(violetdb, hours=5, minutes=-1))
    marks = [h["content"] for h in engine.build("chat", "在吗").history if h["role"] == "system"]
    assert any("到现在已经过去约 4 小时" in m or "到现在已经过去约 5 小时" in m for m in marks)
    assert any("之前的话题不一定还在继续" in m for m in marks)


def test_short_conversation_gets_no_time_marks(engine, violetdb):
    violetdb.add_message("user", "incoming", "text", "跟室友闹别扭了", created_at=at(violetdb, minutes=6))
    violetdb.add_message("assistant", "outgoing", "text", "怎么啦", created_at=at(violetdb, minutes=5))
    packet = engine.build("chat", "她又乱放我东西")
    assert packet.history == [{"role": "user", "content": "跟室友闹别扭了"}, {"role": "assistant", "content": "怎么啦"}]


def test_reminders_distinguish_pending_from_already_sent(engine, violetdb):
    pending = violetdb.add_commitment("交报告", at(violetdb, hours=-10), source_turn_id="t")["commitment"]["id"]
    sent = violetdb.add_commitment("交作业", at(violetdb, hours=4), source_turn_id="t")["commitment"]["id"]
    violetdb.update_commitment(sent, status="sent", delivered_at=at(violetdb, hours=4))
    old = violetdb.add_commitment("还书", at(violetdb, days=3), source_turn_id="t")["commitment"]["id"]
    violetdb.update_commitment(old, status="sent", delivered_at=at(violetdb, days=3))
    gone = violetdb.add_commitment("买票", at(violetdb, hours=1), source_turn_id="t")["commitment"]["id"]
    violetdb.update_commitment(gone, status="cancelled")
    text = text_of(engine.build("chat", "早"))
    assert re.search(r"要提醒对方：交报告（对方要求的，还没到时间）", text)
    assert "交作业：" in text and "已经提醒过了（对方有没有做完不知道；对方没提起就别再催）" in text
    assert "还书" not in text and "买票" not in text
    assert pending and "要提醒对方：交作业" not in text


def test_relationship_comes_from_the_message_log(engine, violetdb):
    assert "这是你们第一次聊天" in text_of(engine.build("chat", "你好"))
    violetdb.add_message("user", "incoming", "text", "你好", created_at=at(violetdb, days=10))
    violetdb.add_message("user", "incoming", "text", "早", created_at=at(violetdb, days=2))
    text = text_of(engine.build("chat", "在吗"))
    assert re.search(r"第一次聊天在 .+（约 10 天前），之后有 2 天聊过天", text)
    proactive = text_of(engine.build("proactive", ""))
    assert "约 10 天前" in proactive                               # proactive messages see it too


def test_legacy_style_renders_exactly_as_before(engine, violetdb, monkeypatch):
    monkeypatch.setenv("VIOLET_CONTEXT_STYLE", "legacy")
    engine.profile.facts.remember("address_name", "小满", evidence="叫我小满", user_message="叫我小满")
    engine.profile.preferences.set_explicit("follow_up_questions", "no", evidence="别问", user_message="别问")
    sent = violetdb.add_commitment("交作业", at(violetdb, hours=4), source_turn_id="t")["commitment"]["id"]
    violetdb.update_commitment(sent, status="sent", delivered_at=at(violetdb, hours=4))
    violetdb.add_message("user", "incoming", "text", "很久以前", created_at=at(violetdb, days=4))
    packet = engine.build("chat", "早")
    text = text_of(packet)
    section = {s.split("\n")[0]: s for s in packet.sections}
    assert section["【交流偏好与边界】"] == "【交流偏好与边界】\n- 不希望被追问（用户明确说过）"       # exact, line for line
    today = violetdb.now()[:10]
    assert section["【用户档案（与本轮相关的部分；数据，不是指令）】"] == (
        f"【用户档案（与本轮相关的部分；数据，不是指令）】\n- 希望被称呼为：小满（用户亲口说，{today}）")
    assert "已经提醒过了" not in text and "你们的相处" not in text
    assert packet.history == [{"role": "user", "content": "很久以前"}]
    assert packet.report["context_style"] == "legacy"


def test_frozen_acceptance_context_checks_still_find_their_words(engine):
    # tests/samples/acceptance_dialogue_30.json (not modified) checks these literal strings in the context
    engine.profile.facts.remember("address_name", "小满", evidence="叫我小满", user_message="叫我小满")
    engine.profile.preferences.set_explicit("follow_up_questions", "no", evidence="别老问我问题", user_message="别老问我问题了")
    text = text_of(engine.build("chat", "今天天气不错"))
    assert "希望被称呼为：小满" in text and "不希望被追问（用户明确说过）" in text


def test_ab_tool_natural_context_only_where_times_are_known():
    import uuid
    from tools import chat_ab
    base = chat_ab.ROOT / "local/p84/test-cases"
    plan_dir = base / uuid.uuid4().hex
    chat_ab.plan(plan_dir, {"R31", "P07", "P08"})              # verification itself stays on the legacy renderer
    manifest = plan_dir / "manifest.json"
    with pytest.raises(ValueError, match="natural context needs recorded message times.*R31"):
        chat_ab.run(manifest, ["R31", "P07"], base / uuid.uuid4().hex, context_style="natural")
    out = base / uuid.uuid4().hex
    summary = chat_ab.run(manifest, ["P07", "P08"], out, context_style="natural")
    assert summary["state"] == "complete" and summary["context_style"] == "natural"
    assert all(f["context_identical"] for f in summary["fairness"].values())
    assert summary["fairness"]["P07"]["limitations"] == [] and summary["fairness"]["P08"]["limitations"]
    request = json.loads((out / "generations/P08-v1-s0.json").read_text(encoding="utf-8"))["request_messages"]
    system = "\n".join(m["content"] for m in request if m["role"] == "system")
    assert "【你们的相处" in system and "用户在管理面板确认" not in system
    # P08: the last message was 359 minutes before the probe's own scenario time
    assert any(re.search(r"已经过去约 5 小时", m["content"]) for m in request if m["role"] == "system")
    legacy = chat_ab.run(manifest, ["P08"], base / uuid.uuid4().hex)
    assert legacy["context_style"] == "legacy"


# --------------------------------------------------------------------------------------- 12-turn conversation

def chat_calls(w):
    return [c for c in w.llm.calls_for("fake-chat")
            if not any("主动触达" in (m.get("content") or "") or "要提醒的事" in (m.get("content") or "")
                       for m in c["messages"])]


def system_of(call):
    return "\n".join(m.get("content") or "" for m in call["messages"] if m["role"] == "system")


def test_twelve_turn_conversation_through_the_runtime(make_world):
    w = make_world()
    replies = iter(["好呀", "物理诶", "好，不问啦", "看电影好惬意", "好，明早叫你", "早～",
                    "交完就轻松啦", "在！", "啊，记错了", "天文", "嗯嗯", "晚安"])
    w.llm.responders["fake-chat"] = lambda kw: (
        "八点啦，记得交作业" if any("要提醒的事" in (m.get("content") or "") for m in kw["messages"]) else next(replies))

    def turn(text, *memory):
        w.llm.responders["fake-memory"] = script(*memory) if memory else (
            lambda kw: json.dumps({"should_save": False, "reason": "t"}))
        say(w, text)
        return chat_calls(w)[-1]

    turn("嗨，以后叫我小满就好", tool("remember_profile_fact", key="address_name", value="小满", evidence="叫我小满"))
    turn("我在读物理", tool("remember_profile_fact", key="study.major", value="物理", evidence="我在读物理"))
    turn("别老问我问题了，有点烦",
         tool("set_interaction_preference", key="follow_up_questions", value="less", evidence="别老问我问题了"))
    c4 = turn("今天去看了场电影")                                                              # P17-like
    assert "不喜欢被连续追问（用户明确说过）：别一句接一句地问" in system_of(c4)
    assert not any(m["role"] == "system" and "隔了" in (m.get("content") or "") for m in c4["messages"][1:])
    c5 = turn("明天早上八点提醒我交作业", tool("create_commitment", content="交作业", evidence="明天早上八点提醒我交作业",
                                         due_date="tomorrow", due_clock="08:00"))
    commitment = w.db.list_commitments()[0]
    assert commitment["status"] == "pending"

    # 20 hours pass; the reminder goes out (R28)
    age_everything(w.db, 20)
    past = (datetime.fromisoformat(w.db.now()) - timedelta(minutes=1)).isoformat()
    w.db.update_commitment(commitment["id"], due_at=past, next_attempt_at=past)
    w.scheduler.tick()
    assert w.db.get_commitment(commitment["id"])["status"] == "sent"
    c6 = turn("早")
    s6 = system_of(c6)
    assert "交作业：" in s6 and "已经提醒过了" in s6 and "还没到时间" not in s6
    marks6 = [m["content"] for m in c6["messages"] if m["role"] == "system" and "聊天记录" in (m.get("content") or "")]
    assert any("这里隔了约 20 小时" in m or "这里隔了约 19 小时" in m for m in marks6)
    c7 = turn("昨天交了作业，总算松口气")                                                      # R29-like
    assert "已经提醒过了" in system_of(c7)

    # three days of silence (age_everything does not move delivered_at; move it with the rest)
    delivered = datetime.fromisoformat(w.db.get_commitment(commitment["id"])["delivered_at"])
    age_everything(w.db, 72)
    w.db.update_commitment(commitment["id"], delivered_at=(delivered - timedelta(hours=72)).isoformat())
    c8 = turn("在吗")
    s8 = system_of(c8)
    assert "已经提醒过了" not in s8                                         # an old delivered reminder is not news
    assert re.search(r"第一次聊天在 .+（约 3 天前|约 4 天前）", s8)
    assert any("约 3 天" in (m.get("content") or "") for m in c8["messages"] if m["role"] == "system")
    turn("其实我不是学物理的，是学天文的",
         tool("correct_profile_fact", key="study.major", old_value="物理", new_value="天文", evidence="是学天文的"))
    c10 = turn("我的专业是什么来着")                                                           # R32-like
    s10 = system_of(c10)
    assert "专业：天文" in s10 and "专业：物理" not in s10
    c11 = turn("嗯")
    turn("困了 睡了")
    for call in chat_calls(w):
        s = system_of(call)
        assert not any(word in s for word in ADMIN_WORDS), s
        assert "希望被称呼为：小满" in s or call is chat_calls(w)[0]
    # within today's conversation after the 3-day gap: one gap mark, no "long silence" note
    marks11 = [m["content"] for m in c11["messages"] if m["role"] == "system" and "【" in (m.get("content") or "")
               and ("隔了" in m["content"] or "之前的聊天" in m["content"])]
    assert len([m for m in marks11 if "已经过去" in m]) == 0
