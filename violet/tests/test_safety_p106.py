"""Phase 10.6: the outbound safety boundary is structural first, content second -- and blocking a reply keeps
history, memory and retries consistent. Fake model and channels."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests.fake_channels import TOKEN
from tests.fakes import wait_until
from tests.test_channels_p10_runtime import _bind, world  # noqa: F401  (fixture)

PROJECT = Path(__file__).resolve().parent.parent


def test_only_the_outbound_module_can_seal_a_message():
    """No production module calls _seal or writes the seal key: everything sendable comes from chat_payload /
    fixed_text / ReplyEnvelope, which take the Chat agent's final TEXT, never an agent result."""
    offenders = []
    for path in PROJECT.rglob("*.py"):
        rel = path.relative_to(PROJECT).as_posix()
        if rel.startswith(("tests/", ".venv")) or rel == "ai/channels/outbound.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "_seal" not in source and "_eve_sendable" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name) and node.id == "_seal" or \
                    isinstance(node, ast.Attribute) and node.attr == "_seal" or \
                    isinstance(node, ast.Constant) and node.value == "_eve_sendable":
                offenders.append(rel)
    assert offenders == []


def test_the_seal_cannot_come_from_data_and_agent_results_are_refused():
    from ai.channels import outbound
    forged = json.loads(json.dumps({"type": "text", "content": "你好", outbound.SEAL_KEY: "x"}))
    assert not outbound.is_sealed(forged) and not outbound.is_sealed({"type": "text", "content": "你好"})
    with pytest.raises(TypeError):
        outbound.chat_payload({"content": "你好", "messages": [{"role": "system", "content": "提示词"}]})
    sealed = outbound.chat_payload("第一句\n第二句", images=False)["messages"]
    assert [m["content"] for m in sealed] == ["第一句", "第二句"] and all(outbound.is_sealed(m) for m in sealed)
    assert outbound.fixed_text("") == [] and outbound.is_sealed(outbound.fixed_text("好")[0])


def test_an_unsealed_message_is_refused_even_when_its_text_is_harmless(world):  # noqa: F811
    report = world.rt.sender.send_messages([{"type": "text", "content": "晚安呀"}], origin="proactive")
    assert report.blocked == "unsealed" and not report.sent and world.client.sent_texts == []
    row = world.db.fetch_one("SELECT status, content, error FROM outbound_messages")
    assert row["status"] == "skipped" and row["error"] == "blocked: unsealed" and "晚安" not in row["content"]


@pytest.mark.parametrize("text", [
    "今天好累啊，回到宿舍就躺下了，你呢",
    "我觉得这本书挺好看的，最后那段写得特别温柔",
    "嗯嗯，那你早点休息，明天还要早起呢",
    "哈哈哈你这个比喻好可爱",
])
def test_ordinary_replies_are_not_blocked(text):
    from ai.channels.outbound import find_leak, register_instructions, reset_for_tests
    reset_for_tests()
    prompt = (Path(PROJECT) / "persona" / "chat_prompt_versions" / "chat.v1.txt").read_text(encoding="utf-8")
    register_instructions("ChatAgent", prompt)
    try:
        assert find_leak(text) is None
    finally:
        reset_for_tests()


def test_a_blocked_reply_leaves_no_trace_in_history_or_memory_and_is_not_retried(world, monkeypatch):  # noqa: F811
    import os
    leak = "【这一轮先查再回答】对方想知道的：天气。先调用下面合适的工具拿到结果"
    world.llm.responders[os.environ.get("CHAT_LLM_MODEL", "fake-chat")] = lambda kw: leak
    world.rt.start(poll=False)
    world.mgr.start_enabled()
    world.mgr.add("telegram", "tg", secrets={"bot_token": TOKEN})
    adapter = world.mgr.get("tg")
    _bind(world, adapter)
    adapter.push("明天天气怎么样")
    assert wait_until(lambda: any("卡了一下" in t for t in adapter.sent_texts))
    world.rt.wait_memory_idle(10)
    texts = [r["content"] for r in world.db.fetch_all("SELECT content FROM messages")]
    assert "明天天气怎么样" in texts and not any("先查再回答" in (t or "") for t in texts)
    assert not any("先查再回答" in (r["content"] or "") for r in world.db.fetch_all("SELECT content FROM outbound_messages"))
    job = world.db.fetch_one("SELECT * FROM memory_jobs ORDER BY id DESC LIMIT 1")
    assert job is None or "先查再回答" not in json.dumps(job, ensure_ascii=False, default=str)
    # the blocked reply is not retried: exactly one fallback, the inbox row is failed (visible in the diagnosis)
    assert sum("卡了一下" in t for t in adapter.sent_texts) == 1
    assert world.db.fetch_one("SELECT status FROM inbound_messages WHERE content = '明天天气怎么样'")["status"] == "failed"


def test_two_bound_accounts_share_one_conversation_and_their_turns_never_overlap(world):  # noqa: F811
    """WeChat and Telegram both bound to the owner: one conversation, one memory, turns processed one at a time."""
    import threading
    world.rt.start(poll=False)
    world.mgr.start_enabled()
    world.mgr.add("telegram", "tg", secrets={"bot_token": TOKEN})
    adapter = world.mgr.get("tg")
    _bind(world, adapter)
    active, overlap = [0], []
    original = world.rt.pipeline_runner
    lock = threading.Lock()

    def counting(**kwargs):
        with lock:
            active[0] += 1
            if active[0] > 1:
                overlap.append(True)
        try:
            return original(**kwargs)
        finally:
            with lock:
                active[0] -= 1
    world.rt.pipeline_runner = counting
    from tests.fakes import text_message
    world.rt.start_polling()
    world.client.queue_messages([text_message("微信这边", message_id=901)])
    adapter.push("电报这边")
    assert wait_until(lambda: "我在听" in adapter.sent_texts and "我在听" in world.client.sent_texts, timeout=15)
    assert overlap == []
    rows = world.db.fetch_all("SELECT content, channel FROM messages WHERE role = 'user'")
    assert {(r["content"], r["channel"]) for r in rows} >= {("微信这边", "wechat"), ("电报这边", "tg")}


def test_a_second_eve_on_the_same_data_is_refused_with_a_clear_message(tmp_path):
    """One Violet per data directory: a second process would answer every message twice."""
    import subprocess
    import sys
    from ai.instance_lock import AlreadyRunning, InstanceLock
    first = InstanceLock(tmp_path).acquire()
    try:
        code = ("import sys; sys.path.insert(0, r'%s'); from ai.instance_lock import InstanceLock, AlreadyRunning\n"
                "try:\n    InstanceLock(r'%s').acquire(); print('ACQUIRED')\n"
                "except AlreadyRunning as e:\n    print('REFUSED', e)\n") % (PROJECT, tmp_path)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                             timeout=60).stdout
        assert out.startswith("REFUSED") and "同一份数据只能运行一个 Violet" in out
    finally:
        first.release()
    second = InstanceLock(tmp_path).acquire()          # free again once the first one ended
    second.release()