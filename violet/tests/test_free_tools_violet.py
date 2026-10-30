"""2026-10-30 after 0.7.14 (user: "比较自由调用工具，在聊天的时候知道要用什么工具"; MCP, web, stickers and images
"发不出来"; the AI persona "被提示词污染"; in groups she only spoke after someone else). Offline; fakes only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fake_mcp import campus_server
from tests.fakes import completion, intent_json
from tests.image_fixtures import PNG
from tests.test_phase2_scenarios import make_world, say  # noqa: F401  (make_world: fixture)
from tests.test_qq_social_violet import FakeAdapter, NoTimer, db, engine  # noqa: F401
from tests.test_tools_p92 import fresh_registry, offered, system, world  # noqa: F401  (fresh_registry: fixture)

WEATHER = "campus__get_weather"


def tool_results(kw) -> list[str]:
    return [m["content"] for m in kw["messages"] if m["role"] == "tool"]


# ------------------------------------------------------------------------------------------------ MCP levels

def test_unclassified_tools_get_a_level_from_their_own_hints(monkeypatch):
    from ai.mcp import classify_tool_permission
    server = {"name": "mine", "transport": "streamable_http"}
    assert classify_tool_permission(server, "look", {"readOnlyHint": True}) == "read"
    assert classify_tool_permission(server, "wipe", {"readOnlyHint": True, "destructiveHint": True}) == "write"
    assert classify_tool_permission(server, "torch_on", {}) == "write"          # says nothing: it may change things
    assert classify_tool_permission({"transport": "inprocess"}, "x", {"readOnlyHint": True}) == "unknown"
    assert classify_tool_permission({**server, "tools": {"look": "write"}}, "look", {"readOnlyHint": True}) == "write"
    monkeypatch.setenv("VIOLET_MCP_AUTO_CLASSIFY", "0")
    assert classify_tool_permission(server, "look", {"readOnlyHint": True}) == "unknown"


def test_a_changing_tool_is_called_in_the_chat_and_waits_for_the_owner(make_world, monkeypatch):
    """No task, no Performer: Chat calls it, the owner is asked in her own reply ("同意 N")."""
    def chat(kw):
        if not tool_results(kw):
            return completion(tool_calls=[{"name": WEATHER, "arguments": {"city": "杭州"}}])
        result = json.loads(tool_results(kw)[-1])
        return f"这个要你点头：回复“同意 {result['approval']}”我就去做" if result.get("error_code") == "NEEDS_APPROVAL" else "?"
    server = campus_server(tools={"get_course_schedule": "read", "get_weather": "write", "search_web": "read"})
    w, chats = world(make_world, monkeypatch, server, intent=lambda kw: intent_json("task", task={
        "exists": True, "name": "查天气", "description": "查天气"}), chat=chat)
    say(w, "帮我看看杭州天气")
    assert WEATHER in offered(chats[0])
    assert w.db.list_task_sessions() == [] and w.llm.calls_for("fake-performer") == []
    number = w.db.fetch_one("SELECT id FROM approvals ORDER BY id DESC LIMIT 1")["id"]
    assert w.client.sent_texts[-1] == f"这个要你点头：回复“同意 {number}”我就去做"


def test_changing_tools_are_never_offered_to_proactive_messages():
    from ai.agents.chat_agent import ChatAgent
    agent = ChatAgent.__new__(ChatAgent)
    agent._config = SimpleNamespace(mcp_servers=[{"name": "campus", "agents": ["chat", "proactive"]}])
    tools = [{"function": {"name": n}} for n in ("campus__get_weather", "campus__torch_on")]
    from ai.agents.base_agent import _CALL_CONTEXT
    token = _CALL_CONTEXT.set({"is_proactive": True,
                               "_mcp_permissions": {"campus__get_weather": "read", "campus__torch_on": "write"}})
    try:
        names = [t["function"]["name"] for t in agent._filter_tools_for_run(tools)]
    finally:
        _CALL_CONTEXT.reset(token)
    assert names == ["campus__get_weather"]


def test_out_of_tool_rounds_she_still_answers(make_world, monkeypatch):
    monkeypatch.setenv("VIOLET_CHAT_MAX_TOOL_ROUNDS", "2")

    def chat(kw):
        if kw.get("tools"):
            return completion(tool_calls=[{"name": WEATHER, "arguments": {"city": "杭州"}}])
        return "查了好几次，先说到这儿"
    w, chats = world(make_world, monkeypatch, campus_server(), intent=lambda kw: intent_json("chat"), chat=chat)
    say(w, "杭州天气")
    assert w.client.sent_texts[-1] == "查了好几次，先说到这儿" and not chats[-1].get("tools")


# ------------------------------------------------------------------------------------------------ stickers

@pytest.fixture()
def clean_stickers(violetdb):
    violetdb.execute("DELETE FROM stickers")
    yield violetdb
    violetdb.execute("DELETE FROM stickers")


def test_sticker_tools_find_send_and_save(clean_stickers, monkeypatch):
    from ai import sticker_tools, stickers
    from ai.images.chat_tools import CompositeGuard
    from ai.images.media import MediaAssetStore
    db = clean_stickers
    assert json.loads(sticker_tools.find(db, {}))["count"] == 0
    cat = stickers.add_bytes(db, PNG, source="upload", description="开心的猫")
    plain = stickers.add_bytes(db, PNG + b"x", source="upload")                     # no description yet
    found = json.loads(sticker_tools.find(db, {"query": "猫"}))
    assert found["count"] == 2 and found["stickers"][0]["number"] == cat["id"]
    assert "还没有说明" in next(s["description"] for s in found["stickers"] if s["number"] == plain["id"])
    guard = sticker_tools.StickerGuard()
    ctx = {"_tool_guard": CompositeGuard(guard)}
    assert json.loads(sticker_tools.send(db, ctx, {"number": plain["id"]}))["ok"]   # sendable without a description
    assert json.loads(sticker_tools.send(db, ctx, {"number": cat["id"]}))["error_code"] == "STICKER_TURN_LIMIT"
    assert json.loads(sticker_tools.send(db, ctx, {"number": 999999}))["error_code"] == "STICKER_NOT_SENDABLE"
    assert CompositeGuard(guard).result_extras()["sticker_ids"] == [plain["id"]]
    assert json.loads(sticker_tools.save(db, {}, {}))["error_code"] in ("NO_RECENT_IMAGE", "SAVE_FAILED")
    asset = MediaAssetStore(db).store_bytes(PNG + b"yz", direction="inbound", media_kind="image", ai_generated=False)
    monkeypatch.setattr(stickers, "describe", lambda path: "主人发的图")
    saved = json.loads(sticker_tools.save(db, {}, {"meaning": "表示无语"}))
    assert saved["ok"] and stickers.get(db, saved["number"])["description"] == "表示无语"
    again = json.loads(sticker_tools.save(db, {}, {"image_number": asset["id"]}))
    assert again["number"] == saved["number"]                                     # the same picture stays one sticker


def test_a_sticker_picked_with_the_tool_goes_out_with_the_reply():
    from ai.channels.outbound import build_reply
    reply = build_reply({"chat": {"content": "哈哈", "sticker_ids": [7]}})
    assert [m.kind for m in reply.messages] == ["text", "sticker"] and reply.messages[1].sticker_id == 7
    alone = build_reply({"chat": {"content": "", "sticker_ids": [7]}})
    assert [m.kind for m in alone.messages] == ["sticker"]
    marked = build_reply({"chat": {"content": "[[表情:3]]", "sticker_ids": [7]}})
    assert [m.sticker_id for m in marked.messages] == [3]                         # the reply's own one wins
    assert not build_reply({"chat": {"content": "[NO_REPLY]", "sticker_ids": [7]}}).should_send
    assert not build_reply({"chat": {"content": "x", "sticker_ids": [7], "failed": True}}).should_send


def test_sticker_tools_are_offered_only_when_they_can_do_something(clean_stickers):
    from ai import stickers
    from ai.agents.base_agent import _CALL_CONTEXT
    from ai.agents.chat_agent import ChatAgent
    from ai.sticker_tools import SCHEMAS
    agent = ChatAgent.__new__(ChatAgent)
    agent._config = SimpleNamespace(mcp_servers=[])
    token = _CALL_CONTEXT.set({"_user_message": "你好"})
    try:
        before = {t["function"]["name"] for t in agent._filter_tools_for_run(list(SCHEMAS))}
        stickers.add_bytes(clean_stickers, PNG, source="upload", description="猫")
        after = {t["function"]["name"] for t in agent._filter_tools_for_run(list(SCHEMAS))}
    finally:
        _CALL_CONTEXT.reset(token)
    assert not before & {"find_stickers", "send_sticker"} and {"find_stickers", "send_sticker"} <= after


def test_a_sticker_may_follow_a_web_answer():
    from ai.web.tool_guard import WEB_SEARCH, WebToolGuard
    guard = WebToolGuard("今天有什么新闻")
    assert guard.check(WEB_SEARCH, {"query": "新闻"}) is None
    guard.observe(WEB_SEARCH, json.dumps({"results": []}))
    assert guard.check("send_sticker", {"number": 1}) is None
    assert json.loads(guard.check("campus__get_weather", {}))["error_code"] == "DENIED_AFTER_WEB"


@pytest.fixture()
def web():
    from tests.web_fixtures import LocalWeb
    w = LocalWeb()
    yield w
    w.close()


def test_short_search_results_come_with_the_first_pages_read(violetdb, web, monkeypatch):
    """User: search "没什么用" -- Bing gives "广州天气预报,广州7天天气预报…" and no number; the pages have them."""
    from ai.web.providers import MockSearchProvider
    from tests.test_web_security import service
    data = [{"title": "广州天气预报", "url": "http://news.example.org/gz", "snippet": "广州天气", "keywords": ["广州", "天气"]},
            {"title": "广州一周天气", "url": "http://books.example.org/gz", "snippet": "一周", "keywords": ["广州", "天气"]},
            {"title": "广州旅游", "url": "http://music.example.org/gz", "snippet": "景点", "keywords": ["广州"]}]
    page = "<html><head><title>广州天气</title></head><body><article><p>今天 多云 23~29℃ 东南风 2 级。</p></article></body></html>"
    web.routes["news.example.org/gz"] = {"body": page}
    web.routes["books.example.org/gz"] = {"status": 500, "body": "err"}
    svc = service(violetdb, web, MockSearchProvider(data))
    out = svc.search_and_read("广州 天气")
    first = next(r for r in out["results"] if r["url"].endswith("news.example.org/gz"))
    assert "23~29℃" in first["page_text"] and out["auto_read"] == 1 and "page_text" in out["note"]
    assert "page_text" not in svc.search("广州 天气")["results"][0]                       # the cache is untouched
    monkeypatch.setenv("VIOLET_WEB_AUTO_READ", "0")
    assert "auto_read" not in svc.search_and_read("广州 天气")
    monkeypatch.setenv("VIOLET_WEB_AUTO_READ", "2")
    monkeypatch.setenv("VIOLET_WEB_AUTO_READ_SNIPPET_CHARS", "1")                          # long enough summaries
    assert "auto_read" not in svc.search_and_read("广州 天气")


# ------------------------------------------------------------------------------------------------ images, plan, list

def test_she_decides_herself_when_a_picture_fits(violetdb, monkeypatch):
    from ai.images.chat_tools import ImageToolGuard
    assert ImageToolGuard(violetdb, "今天天气真好").check("create_image", {"subject": "other"}) is None
    monkeypatch.setenv("VIOLET_IMAGE_SELF_DECIDE", "0")
    assert "IMAGE_NOT_REQUESTED" in ImageToolGuard(violetdb, "今天天气真好").check("create_image", {"subject": "other"})


def test_tool_plan_without_a_task_when_chat_has_the_tools():
    from ai.tool_manager import Catalog, ToolInfo, plan_for_turn
    catalog = Catalog(configured=True, tools={
        "phone__torch_on": ToolInfo("phone__torch_on", "phone", "torch_on", "开手电筒", "write", ["chat", "performer"])})
    task = {"route": "task", "active_task_id": None, "task": {"name": "开手电筒"}}
    assert plan_for_turn(task, catalog)["mode"] == "direct"
    asked = {**task, "tool_query": {"needed": True, "tools": ["phone__torch_on"]}}
    assert plan_for_turn(asked, catalog)["mode"] == "chat"


def test_one_list_of_what_she_can_do(clean_stickers, monkeypatch):
    from ai import stickers
    from ai.tool_manager import Catalog, ToolInfo, abilities_note
    stickers.add_bytes(clean_stickers, PNG, source="upload")
    monkeypatch.setenv("VIOLET_IMAGE_ENABLED", "1")
    monkeypatch.delenv("VIOLET_IMAGE_GEN_MODEL", raising=False)
    catalog = Catalog(configured=True, tools={
        "phone__torch_on": ToolInfo("phone__torch_on", "phone", "torch_on", "开手电筒。", "write", ["chat"],
                                    needs_approval=True)}, unavailable=[{"server": "nas"}])
    note = abilities_note(catalog)
    assert note.startswith("【你现在能做的事】") and "收藏里有 1 个（其中 1 个还没有说明）" in note
    assert "还没有设置画图模型" in note and "工具（要主人同意才会执行）：phone__torch_on" in note
    assert "nas 这次连不上" in note and "现在没有联网工具" in note


# ------------------------------------------------------------------------------------------------ the AI persona

@pytest.fixture()
def ai_persona(tmp_path, monkeypatch):
    from ai import persona_templates
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    data = json.loads((persona_templates.TEMPLATE_DIR / "ai.json").read_text(encoding="utf-8"))
    (tmp_path / "data" / "control").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "control" / "persona_core.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def test_the_ai_persona_speaks_in_the_models_own_way(violetdb, ai_persona, monkeypatch):
    from ai.agents.chat_agent import AI_PROMPT_FILE, ai_prompt
    from ai.context_engine import ContextEngine
    from ai.persona import PersonaCore
    monkeypatch.delenv("CHAT_LLM_PROMPT", raising=False)
    text = PersonaCore.load().render()
    assert "本来的样子" in text and "说话习惯" not in text and "性格：" not in text
    packet = ContextEngine(violetdb).build("chat", "我先去睡啦")              # a "closing": a companion note before
    assert packet.report["ai_natural"] and packet.turn_note is None
    assert "onboarding" not in packet.report["sections"]
    assert ai_prompt() and "本来的方式" in ai_prompt() and AI_PROMPT_FILE.endswith("chat.ai.v1.txt")
    monkeypatch.setenv("CHAT_LLM_PROMPT", "管理员自己的提示词")
    assert ai_prompt() is None                                                   # the admin's prompt is kept
    monkeypatch.setenv("VIOLET_AI_NATURAL", "0")
    assert "说话习惯" in PersonaCore.load().render()
    assert not ContextEngine(violetdb).build("chat", "我先去睡啦").report["ai_natural"]


def test_other_personas_keep_their_voice(violetdb):
    from ai.context_engine import ContextEngine
    packet = ContextEngine(violetdb).build("chat", "我先去睡啦")
    assert not packet.report["ai_natural"] and packet.turn_note


# ------------------------------------------------------------------------------------------------ notices

def test_a_notice_in_her_voice_or_the_plain_text(monkeypatch):
    from ai.chat import run_notice_pipeline

    class Chat:
        def __init__(self, text):
            self.text, self.calls = text, []

        def run(self, message, call_context=None):
            self.calls.append(call_context)
            return {"content": self.text}
    note = "我想开手电筒。回复“同意 3”我就去做，回复“不同意 3”就算了"
    voiced = run_notice_pipeline(note, Chat("想开下手电筒～回“同意 3”我就开，回“不同意 3”就不开"), ["同意 3", "不同意 3"])
    assert not voiced["used_fallback"] and "～" in voiced["messages"][0]["content"]
    lost = run_notice_pipeline(note, Chat("想开下手电筒～"), ["同意 3", "不同意 3"])
    assert lost["used_fallback"] and lost["messages"][0]["content"] == note       # a missing number: the plain text
    monkeypatch.setenv("VIOLET_NOTICE_VOICE", "0")
    chat = Chat("随便")
    assert run_notice_pipeline(note, chat)["messages"][0]["content"] == note and chat.calls == []


# ------------------------------------------------------------------------------------------------ groups

def _group_catalog(monkeypatch):
    import ai.tool_manager as tm
    from ai.tool_manager import Catalog, ToolInfo
    catalog = Catalog(configured=True, tools={
        "violet-web__web_search": ToolInfo("violet-web__web_search", "violet-web", "web_search", "搜索", "read", ["chat"]),
        "android-phone__battery": ToolInfo("android-phone__battery", "android-phone", "battery", "电量", "read", ["chat"]),
        "android-phone__torch_on": ToolInfo("android-phone__torch_on", "android-phone", "torch_on", "手电筒", "write",
                                            ["chat"], needs_approval=True)})
    monkeypatch.setattr(tm, "discover", lambda servers=None: catalog)


def test_group_tools_web_for_everyone_the_owners_data_only_for_him(violetdb, monkeypatch):
    from ai.social.tools import SocialTools
    _group_catalog(monkeypatch)
    monkeypatch.setenv("VIOLET_IMAGE_ENABLED", "1")
    stranger = SocialTools(violetdb, owner_asked=False, turn_key="t")
    owner = SocialTools(violetdb, owner_asked=True, turn_key="t")
    assert stranger.names == {"violet-web__web_search"} and "不是主人" in stranger.note()
    assert owner.names == {"violet-web__web_search", "android-phone__battery", "create_image"}   # never a changing one
    assert json.loads(stranger.run("android-phone__battery", {}))["error_code"] == "UNKNOWN_TOOL"
    monkeypatch.setenv("VIOLET_SOCIAL_TOOLS", "0")
    assert SocialTools(violetdb, owner_asked=True, turn_key="t").names == set()


def test_the_group_decision_looks_it_up_then_answers(monkeypatch):
    from ai.social import llm
    calls = []

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls.append(kw)
                    if len(calls) == 1:
                        return completion(tool_calls=[{"name": "android-phone__battery", "arguments": {}}])
                    return completion('{"action": "reply", "messages": ["你手机还有 76% 的电"]}')
    monkeypatch.setattr(llm, "_client", lambda: Client)
    monkeypatch.setenv("CHAT_LLM_MODEL", "fake-chat")
    tools = SimpleNamespace(schemas=[{"type": "function", "function": {"name": "android-phone__battery"}}],
                            run=lambda name, args: '{"ok": true, "level": 76}')
    decision, model, _ = llm.decide("sys", "user", tools=tools)
    assert decision["messages"] == ["你手机还有 76% 的电"] and calls[0]["tools"]
    assert calls[1]["messages"][-1] == {"role": "tool", "tool_call_id": calls[1]["messages"][-2]["tool_calls"][0]["id"],
                                        "content": '{"ok": true, "level": 76}'}


def test_a_picture_she_drew_goes_to_the_group(db, monkeypatch, tmp_path):
    from ai.images.media import MediaAssetStore
    from ai.social.store import SocialStore
    from tests.test_qq_social_violet import say as decide_with

    class Adapter(FakeAdapter):
        def send_to(self, kind, peer_id, text, reply_to=None, sticker_path=None, image_path=None):
            self.sent.append((kind, peer_id, text, image_path))
            return f"out{len(self.sent)}"
    asset = MediaAssetStore(db).store_bytes(PNG + b"drawn", direction="outbound", media_kind="image", ai_generated=True)

    class Tools:
        def __init__(self, *a, **k):
            self.names, self.image_asset_ids = {"create_image"}, [asset["id"]]

        def note(self):
            return "【这次能用的工具】"
    import ai.social.tools as social_tools
    monkeypatch.setattr(social_tools, "SocialTools", Tools)
    import ai.social.llm as llm
    monkeypatch.setattr(llm, "decide", lambda system, user, tools=None: ({"action": "reply", "messages": ["画好啦"]}, "m", 1))
    conv = SocialStore(db).conversation("napcat", "group", "800", "画画群", default_mode="auto")
    a = Adapter(owners={"9"})
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="800", sender_id="9", sender_name="主人", text="@你 画张自拍", mentioned=True)
    result = e.decide(conv["id"])
    assert result["sent"] == 2 and a.sent[0][2] == "画好啦" and a.sent[1][3].endswith(Path(asset["storage_path"]).name)

def _quiet_group(db, peer="700", quiet_minutes=40, self_minutes=None):
    from ai.social.store import SocialStore
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", peer, "闲聊群", default_mode="auto")
    db.execute("INSERT INTO social_messages (conversation_id, sender_id, sender_name, content, is_self, created_at) "
               "VALUES (?, '1', '小周', '哈哈', 0, datetime(?, ?))",
               (conv["id"], "2026-10-30T15:00:00", f"-{quiet_minutes} minutes"))
    if self_minutes is not None:
        db.execute("INSERT INTO social_messages (conversation_id, content, is_self, created_at) "
                   "VALUES (?, '我来了', 1, datetime(?, ?))", (conv["id"], "2026-10-30T15:00:00", f"-{self_minutes} minutes"))
    return conv


def test_a_quiet_group_may_hear_from_her_first(db, monkeypatch):
    from tests.test_qq_social_violet import say as decide_with
    monkeypatch.setattr(db, "now", lambda: "2026-10-30T15:00:00")
    conv = _quiet_group(db)
    _quiet_group(db, peer="701", quiet_minutes=5)                                  # people are talking right now
    _quiet_group(db, peer="702", quiet_minutes=60 * 24 * 5)                         # nobody spoke for days
    _quiet_group(db, peer="703", self_minutes=30)                                  # she just spoke
    a = FakeAdapter()
    e = engine(db, a)
    assert e.initiative_tick(rand=lambda: 0.0) == [conv["id"]] and NoTimer.made[-1].args == (conv["id"], "idle")
    assert e.initiative_tick(rand=lambda: 0.99) == []
    seen = {}
    decide_with(monkeypatch, {"action": "say", "messages": ["有人玩新出的那个游戏吗"], "reason": "冷场了，想聊聊"}, seen)
    result = e.decide(conv["id"], "idle")
    assert result["sent"] == 1 and "群里安静了一会儿" in seen["user"] and "（没有新消息）" not in seen["user"]
    monkeypatch.setattr(db, "now", lambda: "2026-10-30T23:30:00")
    assert e.initiative_tick(rand=lambda: 0.0) == []                               # not at night
    monkeypatch.setenv("VIOLET_SOCIAL_INITIATIVE", "0")
    monkeypatch.setattr(db, "now", lambda: "2026-10-30T15:00:00")
    assert e.initiative_tick(rand=lambda: 0.0) == []
