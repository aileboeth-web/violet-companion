"""Phase 8.2: one coherent Chat prompt + persona as Violet's own instruction. Offline; no model call."""
import hashlib
import json
from pathlib import Path

import pytest

from tools.harness import _parse_env_file
from tests.test_acceptance import QUIET_MEMORY
from tests.test_phase2_scenarios import make_world, say  # noqa: F401

PROJECT = Path(__file__).resolve().parents[1]
VERSIONS = PROJECT / "persona" / "chat_prompt_versions"
V0_SHA256 = "9f0a6235da2e224714ac21f29bc49f4da16ca9fd76b07f9f109dd6c3160c32e4"   # env_example at e86c23b, renamed to Violet
V1 = (VERSIONS / "chat.v1.txt").read_text(encoding="utf-8")
CORE = json.loads((PROJECT / "persona" / "persona_core.json").read_text(encoding="utf-8"))

# Copied from persona_core.json v1.0.0: the consistency guard's patterns must not change with the voice.
FORBIDDEN_V100 = [
    "我(的|和|跟|家|们家)?(爸|妈|爸爸|妈妈|父母|哥哥|姐姐|弟弟|妹妹|奶奶|爷爷|外婆|外公)",
    "我(的|和|跟)?(男朋友|女朋友|老公|老婆|前任|对象)",
    "我(们)?(的|和|跟|在|去|回)?(公司|单位|老板|同事|领导)",
    "我(们)?(的|和|跟|在|去|回)?(学校|班上|室友|老师|导师|同学)",
    "我(要|得|在|今天|明天|昨天)?(上班|下班|加班|出差|实习)",
    "我(住院|生病了|做手术|搬家了|出国)"]
IDENTITY_V100 = ["我(是|也是)(真人|真实的人|人类|活人)", "我不是(AI|ai|人工智能|虚拟)"]


def test_loaded_prompt_is_v1_and_v0_is_kept_verbatim(monkeypatch):
    from ai.agents.chat_agent import ChatAgent
    monkeypatch.delenv("CHAT_LLM_PROMPT", raising=False)
    assert "CHAT_LLM_PROMPT" not in _parse_env_file(PROJECT / "env_example")      # prompts live in files now
    assert ChatAgent.configured_prompt() == V1.strip()                             # a fresh install loads v1
    v0 = (VERSIONS / "chat.v0.txt").read_text(encoding="utf-8")
    assert hashlib.sha256(v0.encode("utf-8")).hexdigest() == V0_SHA256
    assert "允许露骨内容" in v0 and "找一个轻量借口" in v0          # the record of what was replaced


@pytest.mark.parametrize("removed", [
    "虚拟女友", "像真实亲密关系里的女朋友", "像真人女朋友", "允许露骨", "露骨",
    "可以略微冒犯", "轻微嘴欠", "欠揍", "找一个轻量借口", "臭宝", "宝宝", "吃醋", "不像客服、助理"])
def test_conflicting_or_offensive_licences_are_gone(removed):
    assert removed not in V1


def test_identity_stays_fictional_and_consistent():
    assert "虚构" in V1 and "不假装是现实中的真人" in V1
    assert "虚构" in CORE["identity"]["virtual_declaration"] and "不冒充现实中的真人" in CORE["identity"]["virtual_declaration"]
    assert "真实的身体经历以外" not in CORE["identity"]["virtual_declaration"]   # the old wording leaked into replies


@pytest.mark.parametrize("rule", [
    "不编造对方说过的话", "不给对方安上没提过的习惯或经历", "记不清就直说记不清", "不找理由",
    "不提“资料、记录、系统”之类的东西", "没办成就直说没办成", "不假装成功", "不讲工具、接口、系统、数据库",
    "只有生活记录里标为“已发生”的事才是你做过的事"])
def test_honesty_rules(rule):
    assert rule in V1


@pytest.mark.parametrize("habit", [
    "对方说得短，你通常也短", "不用每次都说得很完整", "对方开玩笑，就顺着玩下去", "不是每句话都需要安慰、关心或建议",
    "对方没问怎么办，就别急着给办法", "不必每次都提问", "你也可以聊自己", "亲近感跟着关系慢慢来",
    "叫对方的名字、语气词和表情都只是偶尔用", "不复述对方刚说的话", "亲密不靠冒犯", "不要说对方敷衍"])
def test_conversation_habits_are_described(habit):
    assert habit in V1


@pytest.mark.parametrize("kept", ["每条微信消息单独占一行", "不要 JSON、Markdown", "只回一句简短的话，不提问"])
def test_operational_format_rules_kept(kept):
    assert kept in V1


def test_persona_core_v110_voice_and_unchanged_guards():
    # phase 8.9.3: the product persona is 1.2.0; the 1.1.0 voice of this phase lives on as a versioned copy
    v110 = json.loads((VERSIONS / "persona_core.v1.1.0.json").read_text(encoding="utf-8"))
    assert v110["version"] == "1.1.0"
    personality = "；".join(v110["personality"])
    for word in ("温柔", "乖巧", "主见", "害羞", "黏人", "撒娇", "小情绪"):
        assert word in personality, word
    for core in (v110, CORE):                                  # the guards never change with the voice
        assert set(core["immutable"]) <= set(core)
        assert core["forbidden_backstory"]["patterns"] == FORBIDDEN_V100
        assert core["identity_violations"]["patterns"] == IDENTITY_V100
        assert "露骨" not in json.dumps(core, ensure_ascii=False)


def test_persona_renders_as_eves_own_instruction():
    from ai.persona import PersonaCore
    text = PersonaCore.load().render()
    header = text.splitlines()[0]
    assert "这就是你自己" in header and f"v{CORE['version']}" in header and "数据，不是对话内容" not in text
    assert "只能把『已发生的生活事件』当成自己的过去经历" in text


def test_chat_request_puts_prompt_first_and_persona_as_instruction(make_world):
    w = make_world(CHAT_LLM_PROMPT=V1, VIOLET_PERSONA_CONTEXT="1")
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    w.llm.responders["fake-chat"] = lambda kw: "哈哈哈食堂就是开盲盒"
    say(w, "今天食堂的饭好难吃")
    messages = w.llm.calls_for("fake-chat")[-1]["messages"]
    assert messages[0] == {"role": "system", "content": V1.strip()}
    persona = [i for i, m in enumerate(messages) if m["role"] == "system" and "这就是你自己" in (m.get("content") or "")]
    assert len(persona) == 1 and persona[0] > 0
    assert messages[-1] == {"role": "user", "content": "今天食堂的饭好难吃"}
    assert not any("数据，不是对话内容" in (m.get("content") or "") for m in messages)
    assert w.client.sent_texts[-1] == "哈哈哈食堂就是开盲盒"


def test_identity_guard_still_catches_claims_of_being_human():
    import re
    assert any(re.search(p, "我是真人啦") for p in CORE["identity_violations"]["patterns"])


def test_evaluation_set_v1_and_rules_untouched():
    from tools import chat_eval
    raw = (chat_eval.ROOT / chat_eval.SET_PATH).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "c51c42b54d24b07730da23e88000f46cf083ed1e2f74723c87c2cfc3d395b80d"
    assert list(chat_eval.DIMENSIONS) == ["natural", "context", "grounded", "rhythm", "self", "intimacy", "respect"]
