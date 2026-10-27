"""Phase 8.8: interaction signals and context reliability. Offline: these prove the mechanism (what Violet is
given, what is stored, when one rewrite happens), not that real replies became natural."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest

from ai import interaction as ia
from tests.test_phase2_scenarios import make_world, say, script, tool  # noqa: F401

NOW = datetime.fromisoformat("2026-11-07T09:30:00+08:00")


@pytest.fixture()
def engine(violetdb):
    from ai.context_engine import ContextEngine
    return ContextEngine(violetdb)


def text_of(packet):
    return "\n".join(packet.sections)


def section(packet, header):
    return next((s for s in packet.sections if s.startswith(header)), "")


# --------------------------------------------------------------------------------------- unit: turn signals

@pytest.mark.parametrize("text,kind", [
    ("在吗", "open"), ("干嘛呢", "open"), ("在干嘛呢", "open"), ("好无聊", "open"), ("随便聊点什么吧", "open"),
    ("想听你说说话", "open"), ("你有什么想问我的吗", "invite"), ("你问吧", "invite"),
    ("你觉得早起的人是不是都很自律", "question"), ("你还记得我学什么的吗", "question"),
    ("今天去看了场电影", "share"), ("晚上吃了火锅", "share"), ("嗯", "ack"),     # 8.9: a bare "嗯" is its own kind
])
def test_turn_kind(text, kind):
    assert ia.turn_kind(text) == kind


@pytest.mark.parametrize("text,route,focus", [
    ("在吗", None, False), ("干嘛呢", None, False), ("你今天干嘛了", None, False), ("你还记得我学什么的吗", None, False),
    ("今天组会被导师当着所有人说 好委屈", None, True), ("我好焦虑", None, True), ("帮我查一下", "task", True),
    ("今天组会被导师当着所有人说数据做得一塌糊涂 我真的准备了很久", None, True),
])
def test_needs_focus_is_about_weight_not_question_marks(text, route, focus):
    assert ia.needs_focus(text, route) is focus


@pytest.mark.parametrize("text,violet", [
    ("你也去爬过山吗", True), ("你喜欢吃烤肉吗", True), ("你觉得早起的人是不是都很自律", True), ("在吗", True),
    ("你还记得我学什么的吗", False), ("今天去看了场电影", False), ("你有什么想问我的吗", False),
])
def test_asks_about_eve(text, violet):
    assert ia.asks_about_eve(text) is violet


@pytest.mark.parametrize("text,end", [
    ("早 今天一整天都要在实验室", NOW.replace(hour=23, minute=59, second=0)),
    ("今天都在开会", NOW.replace(hour=23, minute=59, second=0)),
    ("这两天都会很忙 别等我啊", (NOW + timedelta(days=1)).replace(hour=23, minute=59, second=0)),
    ("这几天要出差", (NOW + timedelta(days=2)).replace(hour=23, minute=59, second=0)),
    ("下午还有个会 我先去了", None), ("等我一会儿", None), ("在开会", None),
])
def test_stated_busy_period(text, end):
    assert ia.stated_busy_until(text, NOW) == end


def test_follow_up_preference_and_violation():
    assert ia.follow_up_preference({"follow_up_questions": {"value": "no", "origin": "implicit"}}) == "no"
    assert ia.follow_up_preference({"follow_up_questions": {"value": "less", "origin": "explicit"}}) == "less"
    assert ia.follow_up_preference({"follow_up_questions": {"value": "less", "origin": "implicit"}}) is None
    assert ia.follow_up_preference({}) is None
    v = ia.follow_up_violation
    assert v("看了什么电影呀？", "今天去看了场电影", "no") and v("好看吗～", "今天去看了场电影", "no")
    assert v("挺好的\n那你现在在家吗", "今天去看了场电影", "less")
    assert not v("真好，我也想找部片子看", "今天去看了场电影", "no")
    assert not v("我觉得不一定\n你觉得呢？", "你觉得早起的人是不是都很自律", "no")       # the user asked
    assert not v("那你最近在忙什么呀？", "你有什么想问我的吗", "no")                     # the user invited it
    assert not v("看了什么电影呀？", "今天去看了场电影", None)                           # no preference


def test_turn_note_only_where_a_signal_applies():
    assert "不喜欢被追问" in ia.turn_note("今天去看了场电影", "no")
    assert "请你问问题" in ia.turn_note("你有什么想问我的吗", None) and "请你问问题" in ia.turn_note("你有什么想问我的吗", "no")
    assert ia.turn_note("你觉得早起的人是不是都很自律", "no") is None
    assert ia.turn_note("今天去看了场电影", None) is None


# --------------------------------------------------------------------------------------- ContextEngine (natural)

def test_corrected_major_reaches_chat_when_the_user_asks(engine, monkeypatch):
    facts = engine.profile.facts
    facts.remember("address_name", "小满", evidence="叫我小满", user_message="叫我小满")
    facts.remember("study.major", "天文", evidence="学天文", user_message="我学天文")
    facts.remember("study.major", "物理", evidence="是学物理的", user_message="其实我不是学天文的 是学物理的",
                   correction=True, replaces_value="天文")
    # one phrasing per mechanism: self-recall alone ("你还记得我吗"), asked_as alone ("猜猜我是学什么的"),
    # both ("你还记得我学什么的吗"), the old topic word ("专业")
    for question in ("你还记得我吗", "猜猜我是学什么的", "你还记得我学什么的吗", "我读的什么专业"):
        profile = section(engine.build("chat", question), "【你了解的对方")
        assert "专业：物理" in profile and "天文" not in profile, question
    assert "专业" not in section(engine.build("chat", "今天天气不错"), "【你了解的对方")     # not on unrelated turns
    monkeypatch.setenv("VIOLET_CONTEXT_STYLE", "legacy")
    legacy = engine.build("chat", "你还记得我学什么的吗")
    assert "专业" not in section(legacy, "【用户档案")                                     # the 8.7 E3 failure, kept in legacy


def test_follow_up_preference_suppresses_onboarding_offers(engine, violetdb, monkeypatch):
    assert "【可选的了解】" in text_of(engine.build("chat", "今天天气不错"))               # control: offered
    violetdb.execute("DELETE FROM profile_questions")
    engine.profile.preferences.set_explicit("follow_up_questions", "no", evidence="别老问我问题",
                                            user_message="别老问我问题了")
    packet = engine.build("chat", "今天天气不错")
    assert "【可选的了解】" not in text_of(packet) and packet.report["onboarding_key"] is None
    assert engine.onboarding._last_offer() is None                                        # nothing recorded as offered
    monkeypatch.setenv("VIOLET_CONTEXT_STYLE", "legacy")
    assert "【可选的了解】" in text_of(engine.build("chat", "今天天气不错"))               # legacy renders as before


@pytest.fixture()
def violet_life(violetdb):
    from ai.life_store import LifeStore
    store = LifeStore(violetdb)
    day = (datetime.fromisoformat(violetdb.now()) - timedelta(days=1)).isoformat()
    store.insert_event(uid=f"t-{uuid.uuid4().hex}", thread_id=None, plan_id=None, event_type="progress",
                       content="又往后读了几章《夜晚的潜水艇》", occurred_at=day, duration_minutes=60, source="test")
    return store


@pytest.mark.parametrize("message,shown", [
    ("在吗", "life"), ("干嘛呢", "life"), ("好无聊", "life"), ("你也去爬过山吗", "life"), ("那本书讲的啥", "life"),
    ("今天被导师当众批评了 好委屈", "focus"), ("今天去看了场电影", "none"), ("你还记得我学什么的吗", "none"),
])
def test_life_section_for_greetings_and_questions_about_eve(engine, violet_life, message, shown):
    life = section(engine.build("chat", message), "【Violet 的生活状态")
    assert ("夜晚的潜水艇" in life) is (shown == "life")
    assert ("不要主动谈你自己的生活" in life) is (shown == "focus")
    assert (life == "") is (shown == "none")


def test_greeting_was_a_heavy_moment_in_legacy(engine, violet_life, monkeypatch):
    monkeypatch.setenv("VIOLET_CONTEXT_STYLE", "legacy")
    assert "不要主动谈你自己的生活" in section(engine.build("chat", "在吗"), "【Violet 的生活状态")


def test_turn_note_is_built_for_chat_only(engine, monkeypatch):
    engine.profile.preferences.set_explicit("follow_up_questions", "no", evidence="别老问我问题",
                                            user_message="别老问我问题了")
    assert "不喜欢被追问" in engine.build("chat", "今天去看了场电影").turn_note
    assert engine.build("chat", "你觉得呢").turn_note is None
    assert "请你问问题" in engine.build("chat", "你有什么想问我的吗").turn_note
    assert engine.build("proactive", "").turn_note is None
    monkeypatch.setenv("VIOLET_CONTEXT_STYLE", "legacy")
    assert engine.build("chat", "今天去看了场电影").turn_note is None


# --------------------------------------------------------------------------------------- situations

@pytest.fixture()
def clock_at_now():
    """Violet's clock at NOW (a Saturday morning). Phase 9.2.1: the busy test read the real clock, and after about 21:00
    Asia/Shanghai "今天一整天" has less than 3 hours left, so the same assertions failed in the evening."""
    from tools.harness import ScenarioClock, install_clock, uninstall_clock
    saved = install_clock(ScenarioClock(NOW))
    yield
    uninstall_clock(saved)


@pytest.mark.parametrize("words,until,hours", [
    ("今天一整天都要在实验室", None, None), ("这两天都会很忙", None, None), ("下午还有个会 我先去了", None, 3),
    ("今天一整天都要在实验室", "1h", None), ("今天一整天都要在实验室", "tomorrow", None),
])
def test_busy_lasts_as_long_as_the_user_said(clock_at_now, engine, violetdb, words, until, hours):
    from ai.memory_model import parse_time
    store = engine.profile.situations
    now = parse_time(violetdb.now())
    got = store.observe("availability", "在忙，没空聊天", "user_stated", evidence=words, user_message=words, until=until)
    expires = parse_time(got["situation"]["expires_at"])
    if hours:
        assert abs((expires - now) - timedelta(hours=hours)) < timedelta(minutes=1)
    elif until == "tomorrow":
        assert expires.date() == (now + timedelta(days=1)).date()                   # an explicit longer until wins
    else:
        assert expires == ia.stated_busy_until(words, now) and expires - now > timedelta(hours=3)


def test_a_mood_is_not_stretched_by_busy_words(engine, violetdb):
    from ai.memory_model import parse_time
    got = engine.profile.situations.observe("mood", "有点累", "user_stated", evidence="今天一整天都好累",
                                           user_message="今天一整天都好累")
    assert parse_time(got["situation"]["expires_at"]) - parse_time(violetdb.now()) <= timedelta(hours=6, minutes=1)


# --------------------------------------------------------------------------------------- runtime

def chat_texts(w):
    return [c for c in w.llm.calls_for("fake-chat")]


def test_follow_up_rewrite_through_the_runtime(make_world):
    w = make_world()
    rewritten = []

    def chat(kw):
        system = "\n".join(m.get("content") or "" for m in kw["messages"] if m["role"] == "system")
        if "上一版回复以提问结尾" in system:
            return rewritten.pop(0)
        user = kw["messages"][-1]["content"]
        return {"今天去看了场电影": "看了什么电影呀？", "晚上吃了火锅": "吃的什么锅底呀？",
                "你有什么想问我的吗": "那你最近在学什么呀？", "嗯嗯": "好"}.get(user, "好呀")
    w.llm.responders["fake-chat"] = chat
    w.llm.responders["fake-memory"] = script(tool("set_interaction_preference", key="follow_up_questions", value="no",
                                                  evidence="别老问我问题了"))
    say(w, "别老问我问题了，有点烦")
    w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})
    calls = len(chat_texts(w))
    rewritten.append("真好，我也想找部片子窝着看")
    say(w, "今天去看了场电影")
    assert len(chat_texts(w)) == calls + 2 and w.client.sent_texts[-1] == "真好，我也想找部片子窝着看"
    first, second = chat_texts(w)[-2:]
    assert any("不喜欢被追问" in (m.get("content") or "") for m in first["messages"][-2:])     # the turn note
    calls = len(chat_texts(w))
    rewritten.append("那是什么锅底呢")                                                         # still asks: original kept
    say(w, "晚上吃了火锅")
    assert len(chat_texts(w)) == calls + 2 and w.client.sent_texts[-1] == "吃的什么锅底呀？"
    calls = len(chat_texts(w))
    say(w, "你有什么想问我的吗")                                                                 # invited: no rewrite
    assert len(chat_texts(w)) == calls + 1 and w.client.sent_texts[-1] == "那你最近在学什么呀？"


def test_no_rewrite_without_the_preference_or_when_switched_off(make_world, monkeypatch):
    w = make_world()
    w.llm.responders["fake-chat"] = lambda kw: "看了什么电影呀？"
    say(w, "今天去看了场电影")
    assert len(w.llm.calls_for("fake-chat")) == 1
    w.db.execute("INSERT INTO interaction_preferences (pref_key, value, origin, evidence, confidence, observations, "
                 "status, created_at, updated_at) VALUES ('follow_up_questions', 'no', 'explicit', 'x', 1, 0, 'active', ?, ?)",
                 (w.db.now(), w.db.now()))
    monkeypatch.setenv("VIOLET_FOLLOW_UP_CHECK", "0")
    say(w, "今天去看了场电影")
    assert len(w.llm.calls_for("fake-chat")) == 2 and w.client.sent_texts[-1] == "看了什么电影呀？"


def test_intent_sees_the_last_messages_with_times(make_world, monkeypatch):
    w = make_world()
    say(w, "我要睡了")
    say(w, "晚安")
    intent = w.llm.calls_for("fake-intent")[-1]["messages"]
    note = next(m["content"] for m in intent if (m.get("content") or "").startswith("【最近的聊天"))
    assert "用户：我要睡了" in note and "Violet：" in note and intent[-1] == {"role": "user", "content": "晚安"}
    monkeypatch.setenv("VIOLET_INTENT_RECENT_MESSAGES", "0")
    say(w, "嗯嗯")
    assert not any((m.get("content") or "").startswith("【最近的聊天") for m in w.llm.calls_for("fake-intent")[-1]["messages"])


def test_reminders_still_go_out_while_the_user_is_busy(make_world):
    w = make_world()
    w.llm.responders["fake-chat"] = lambda kw: (
        "到点啦，交作业" if any("要提醒的事" in (m.get("content") or "") for m in kw["messages"]) else "好")
    w.llm.responders["fake-memory"] = script(tool("set_situation", kind="availability", value="今天一整天在实验室，没空",
                                                  evidence="今天一整天都要在实验室"))
    say(w, "今天一整天都要在实验室")
    assert w.scheduler._busy_situation() is not None
    past = (datetime.fromisoformat(w.db.now()) - timedelta(minutes=1)).isoformat()
    cid = w.db.add_commitment("交作业", past, source_turn_id="t")["commitment"]["id"]
    w.db.update_commitment(cid, next_attempt_at=past)
    w.scheduler.tick()
    assert w.db.get_commitment(cid)["status"] == "sent" and w.client.sent_texts[-1] == "到点啦，交作业"


# --------------------------------------------------------------------------------------- 8.7 episodes, offline

@pytest.fixture(scope="module")
def episodes_run():
    from tools import chat_dialogue as cd
    out = cd.ROOT / "local/p88/test-cases" / uuid.uuid4().hex
    summary = cd.run(out, label="p88", episode_ids=["E1", "E3", "E4", "E5", "E6", "E8"])
    return summary, json.loads((out / "debug.json").read_text(encoding="utf-8"))["episodes"]


def request_of(step, agent):
    return next(r["request"] for r in step["requests"] if r["agent"] == agent)


def system(step, agent="ChatAgent"):
    return "\n".join(m["content"] for m in request_of(step, agent) if m["role"] == "system")


def test_original_episodes_still_complete(episodes_run):
    summary, _ = episodes_run
    assert summary["state"] == "complete" and summary["steps_executed"] == summary["steps_planned"]


def test_e3_corrected_major_and_reminder(episodes_run):
    steps = episodes_run[1]["E3"]["steps"]
    assert "专业：物理" in system(steps[9]) and "专业：天文" not in system(steps[9])
    assert steps[5]["reminders"][0]["delivery"] == "sent" and "已经提醒过了" in system(steps[6])


def test_e5_e4_eve_has_her_own_day_on_open_turns(episodes_run):
    e5 = episodes_run[1]["E5"]["steps"]
    for n in (0, 1):                                                     # 好无聊 / 随便聊点什么吧
        assert "夜晚的潜水艇" in system(e5[n]) and "不要主动谈你自己" not in system(e5[n])
    assert "边城" in system(episodes_run[1]["E4"]["steps"][0])            # 在干嘛呢


def test_e6_busy_all_day_blocks_the_proactive_check(episodes_run):
    steps = episodes_run[1]["E6"]["steps"]
    situation = steps[0]["state_changes"]["situations"]["now"][0]
    assert situation["expires_at"].startswith("2026-11-07T23:59")
    assert steps[2]["kind"] == "scheduler" and "busy" in steps[2]["due_reason"] and not steps[2]["proactive_sent"]


def test_e8_preference_and_invitation(episodes_run):
    steps = episodes_run[1]["E8"]["steps"]
    first = request_of(steps[0], "ChatAgent")
    assert "【可选的了解】" not in system(steps[0])
    assert "不喜欢被追问" in first[-2]["content"] and first[-1]["content"] == "今天去看了场电影"
    assert not any(m["content"].startswith("【这一句】") for m in request_of(steps[4], "ChatAgent"))   # a question
    assert "请你问问题" in request_of(steps[6], "ChatAgent")[-2]["content"]                          # invitation


def test_e1_intent_sees_the_goodbye(episodes_run):
    steps = episodes_run[1]["E1"]["steps"]
    note = next(m["content"] for m in request_of(steps[8], "IntentAgent") if m["content"].startswith("【最近的聊天"))
    assert "用户：晚安" in note and request_of(steps[8], "IntentAgent")[-1]["content"] == "嗯嗯"
