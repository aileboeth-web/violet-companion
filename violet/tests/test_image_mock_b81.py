"""Phase 7 B.8.1: --image-mock -- pictures through the real Violet pipeline with ONLY the offline mock image
adapter; the text model stays whatever the run uses. Network, provider and sender isolation are enforced
by the program and tested here, offline: fictional data, scripted chat model, temp directories."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from tests.fakes import completion

PROJECT = Path(__file__).resolve().parent.parent
USER_21 = "给我看看你现在在干嘛"
USER_22 = "这张是你拍的吗"


@pytest.fixture()
def clean_env():
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


class ChatScript:
    """What the scripted chat model does: draw (call create_image) and what it says afterwards."""

    def __init__(self):
        self.draw = True
        self.after_ok = "给你画了一张，是 AI 画的插画，不是拍的"
        self.after_fail = "这次没画出来，抱歉"
        self.plain = "嗯嗯"
        self.offered: list[list[str]] = []


@pytest.fixture()
def script(monkeypatch):
    from tools import harness
    s = ChatScript()
    original = harness.MockLLM._create

    def scripted(self, **kwargs):
        if harness._AGENT.get() != "ChatAgent":
            return original(self, **kwargs)
        model = str(kwargs.get("model"))
        tools = [t["function"]["name"] for t in kwargs.get("tools") or []]
        s.offered.append(tools)
        tool_msgs = [m for m in kwargs.get("messages") or [] if m.get("role") == "tool"]
        if s.draw and "create_image" in tools and not tool_msgs:
            return completion(tool_calls=[{"name": "create_image", "arguments": {"subject": "violet", "scene": "current"}}],
                              model=model)
        if tool_msgs:
            payload = json.loads(tool_msgs[-1]["content"])
            if payload.get("ok"):
                return harness.MockLLM._completion(f"{s.after_ok}\n{payload['image_marker']}", model)
            return harness.MockLLM._completion(s.after_fail, model)
        return harness.MockLLM._completion(s.plain, model)
    monkeypatch.setattr(harness.MockLLM, "_create", scripted)
    return s


def _plan():
    from tools.replay import load_plan
    return load_plan(PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json")[0]


@pytest.fixture()
def session(clean_env, violetdb, tmp_path, monkeypatch):
    from tools.dialogue_eval import preload
    from tools.violet_test_chat import build_parser, open_session
    s = open_session(build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-img"), "--image-mock",
                                                "--quiet-hours", "off"]), out=lambda x: None, db=violetdb)
    # Phase 8.9.1: open_session clears VIOLET_*, so the ordinary day (wall clock) came back on; on a day with an item
    # in the last 12 hours the "current" picture became that item. Off, as dialogue_eval does for the 32-round plan.
    monkeypatch.setenv("VIOLET_DAILY_LIFE", "0")
    preload(s, _plan())                                       # Violet's life: reading 《边城》 (the "current" scene)
    yield s
    s.close()


def _round(session, text, n):
    from tools.dialogue_eval import picture_checks, picture_facts, picture_premise, picture_reply_flags
    plan = {r["n"]: r for r in _plan()["rounds"]}
    premise = picture_premise(session.db) if plan.get(n - 1, {}).get("image") else None
    result = session.say(text, n)
    facts = picture_facts(result["images"], result["calls"])
    checks = picture_checks(plan[n], facts, premise, picture_reply_flags(result["violet"], facts))
    return result, facts, checks


# ---------------------------------------------------------------------------
# Default: images stay off
# ---------------------------------------------------------------------------

def test_without_the_flag_images_stay_off(clean_env, violetdb, tmp_path):
    from tools.violet_test_chat import build_parser, open_session
    connect_before = socket.socket.connect
    s = open_session(build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-off")]), out=lambda x: None, db=violetdb)
    try:
        from ai.images.service import images_enabled
        assert os.environ["VIOLET_IMAGE_ENABLED"] == "0" and not images_enabled()
        assert s.network_guard is None and s.image_mock is False
        with pytest.raises(RuntimeError, match="images are disabled"):
            s.wechat.send_image("x.png")
        assert socket.socket.connect is connect_before              # no guard of the test tool installed
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Round 21: the whole chain, offline
# ---------------------------------------------------------------------------

def test_round21_draws_stores_and_records_a_mock_picture(session, script, tmp_path):
    result, facts, checks = _round(session, USER_21, 21)
    assert facts["tool_called"] and facts["generated"] and facts["violet_scene"] and facts["sent_recorded"]
    assert facts["provider"] == "mock" and facts["non_mock_calls"] == []
    assert checks["results"] == {"tools_has:create_image": True, "db:violet_picture_sent": True}
    assert checks["failures"] == {} and checks["valid"] is True
    images = result["images"]
    asset = images["assets"][0]
    assert asset["ai_generated"] and asset["provider_id"] == "mock" and asset["scene_kind"] == "current_thread"
    assert images["calls"] == [{"entry_id": "mock/mock-image-1", "status": "ok", "accepted": "yes", "error_code": None}]
    sent = images["sent"][0]
    saved = Path(session.workdir) / "sent_images" / sent["file"]
    assert saved.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and sent["sha256"] == asset["sha256"] == \
        hashlib.sha256(saved.read_bytes()).hexdigest()
    assert sent["round"] == 21 and sent["sender"].startswith("local test sender")
    log = [json.loads(line) for line in (saved.parent / "sent_images.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log == [sent]
    assert result["violet"][0].startswith("给你画了一张") and "[图片" not in "".join(result["violet"])
    guard = session.network_guard.summary()
    assert guard["installed"] and guard["blocked_connections"] == [] and guard["allowed_endpoints"] == []
    assert any("create_image" in tools for tools in script.offered)


def test_round22_has_its_premise_only_after_a_picture_was_really_sent(session, script):
    script.draw = False
    script.plain = "不是拍的，是 AI 画的插画"
    _, facts, checks = _round(session, USER_22, 22)
    assert checks["premise"]["met"] is False and checks["valid"] is False and "premise" in checks["failures"]
    script.draw = True
    _round(session, USER_21, 21)
    script.draw = False
    result, facts, checks = _round(session, USER_22, 22)
    assert checks["premise"]["met"] is True and checks["premise"]["provider_id"] == "mock"
    assert checks["results"] == {"tools_lacks:create_image": True} and checks["failures"] == {} and checks["valid"]
    assert "create_image" not in script.offered[-1]            # "这张是你拍的吗" does not ask for a new picture


def test_premise_comes_from_the_database_in_a_later_session(session, script, violetdb, clean_env, tmp_path):
    from tools.dialogue_eval import picture_premise
    _round(session, USER_21, 21)
    session.close()
    premise = picture_premise(violetdb)                           # e.g. continued with --seed-from in another run
    assert premise["met"] and premise["provider_id"] == "mock"


# ---------------------------------------------------------------------------
# Failures are never counted as success
# ---------------------------------------------------------------------------

def _set_mock_options(session, **options):
    from tools.harness import write_mock_image_config
    write_mock_image_config(session.workdir, options)


@pytest.mark.parametrize("code", ["connect", "unknown_format", "http_500"])
def test_a_failed_generation_is_no_picture_and_never_reaches_another_provider(session, script, code):
    from tools.harness import RefusedImageAdapter
    RefusedImageAdapter.attempts.clear()
    _set_mock_options(session, fail=[code])
    result, facts, checks = _round(session, USER_21, 21)
    assert facts["tool_called"] and not facts["generated"] and not facts["sent_recorded"]
    assert checks["results"]["db:violet_picture_sent"] is False
    assert "Mock 生成成功" in checks["failures"]["db:violet_picture_sent"]
    assert [c["entry_id"] for c in result["images"]["calls"]] == ["mock/mock-image-1"]       # no fallback
    assert RefusedImageAdapter.attempts == [] and session.network_guard.summary()["blocked_connections"] == []
    assert result["images"]["sent"] == [] and result["violet"] == ["这次没画出来，抱歉"]


def test_saying_a_picture_was_sent_when_it_failed_is_flagged(session, script):
    _set_mock_options(session, fail=["unknown_format"])
    script.after_fail = "给你画好了，看这张"
    _, facts, checks = _round(session, USER_21, 21)
    assert not facts["generated"] and checks["failures"]["reply:claims_picture_without_one_sent"]


def test_a_failed_send_is_not_a_sent_picture(session, script):
    session.wechat.fail_images = 99
    result, facts, checks = _round(session, USER_21, 21)
    assert facts["generated"] and not facts["sent_recorded"]
    assert "测试发送器记录了这张图" in checks["failures"]["db:violet_picture_sent"]
    assert [o["status"] for o in result["images"]["outbound"]] == ["failed"]
    assert result["images"]["sent"] and not any(x["recorded"] for x in result["images"]["sent"])
    assert not (Path(session.workdir) / "sent_images").exists() or not list((Path(session.workdir) / "sent_images").glob("*.png"))
    assert "reply:claims_picture_without_one_sent" in checks["failures"]          # "给你画了一张" without a picture
    from tools.dialogue_eval import picture_premise
    assert picture_premise(session.db)["met"] is False                           # nothing was sent: no premise


@pytest.mark.parametrize("line,flag", [
    ("这是我刚拍的照片", "claims_photo_or_camera"),
    ("这是一张真实的照片", "claims_photo_or_camera"),
    ("这不是拍的，是 AI 画的插画", None),
])
def test_a_picture_told_as_a_photo_never_reaches_the_user_unflagged(session, script, line, flag):
    """The product's reply guard (ai/chat.py) already removes or rewrites camera claims; whatever still
    reaches the user is flagged by the evaluation."""
    from ai.consistency import CAMERA_CLAIM_RE, CAMERA_NEGATED_RE
    from tools.dialogue_eval import picture_reply_flags
    script.after_ok = line
    result, facts, checks = _round(session, USER_21, 21)
    assert facts["sent_recorded"]
    claims = [l for l in result["violet"] if CAMERA_CLAIM_RE.search(l) and not CAMERA_NEGATED_RE.search(l)]
    assert not claims or "reply:claims_photo_or_camera" in checks["failures"]
    assert picture_reply_flags([line], facts) == ([flag] if flag else [])          # the detector itself


def test_round22_calling_create_image_again_fails_its_check():
    from tools.dialogue_eval import picture_checks
    plan = {r["n"]: r for r in _plan()["rounds"]}
    facts = {"tool_called": True, "generated": True, "violet_scene": True, "sent_recorded": True, "provider": "mock",
             "non_mock_calls": []}
    checks = picture_checks(plan[22], facts, {"required": True, "met": True}, [])
    assert checks["results"] == {"tools_lacks:create_image": False} and "tools_lacks:create_image" in checks["failures"]
    other = picture_checks(plan[21], {**facts, "non_mock_calls": ["sub2api/gpt-image-1"]}, None, [])
    assert "image_provider" in other["failures"]


# ---------------------------------------------------------------------------
# Provider and network isolation (program-enforced)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("edit,message", [
    (lambda d: d["providers"][0].update(adapter_type="openai_images"), "adapter_type"),
    (lambda d: d["providers"][0].update(base_url="https://img.example.org/v1"), "endpoint or a key"),
    (lambda d: d["providers"][0]["models"][0].update(api_key_env="LLM_API_KEY"), "endpoint or a key"),
    (lambda d: d["providers"].append({"provider_id": "sub2api", "adapter_type": "openai_images", "models": [
        {"model_id": "gpt-image-1", "enabled": True, "capabilities": {"image_generation": "verified"}}]}), "sub2api"),
    (lambda d: d["providers"][0]["models"][0]["options"]["mock"].update({"return": "url"}), "URL outputs"),
    (lambda d: d["providers"][0]["models"][0].update(enabled=False), "no enabled mock model"),
])
def test_anything_but_the_offline_mock_is_refused(tmp_path, clean_env, edit, message):
    from tools.harness import IsolationError, validate_image_config, write_mock_image_config
    path = write_mock_image_config(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(IsolationError, match=message):
        validate_image_config(path)


def test_a_real_provider_in_the_configuration_stops_the_run_before_anything(clean_env, violetdb, tmp_path, monkeypatch, capsys):
    from tools import dialogue_eval, violet_test_chat, harness
    original = harness.write_mock_image_config

    def with_real_provider(workdir, options=None):
        path = original(workdir, options)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["providers"][0]["adapter_type"] = "openai_images"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path
    monkeypatch.setattr(violet_test_chat, "write_mock_image_config", with_real_provider)
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input, **kw: real_open(args, input_fn=input_fn, out=lambda s: None, db=violetdb, **kw))
    target = tmp_path / "violet-testchat-realimg"
    rc = dialogue_eval.main(["--rounds", "21", "--image-mock", "--workdir", str(target), "--output-dir", str(tmp_path / "o")])
    assert rc == 2 and "只允许离线 Mock 图片模型" in capsys.readouterr().out and not target.exists()


def test_non_mock_adapters_and_image_http_are_refused_while_locked(clean_env):
    import httpx
    from ai.images import adapters
    from ai.images.registry import ModelEntry
    from tools.harness import RefusedImageAdapter, lock_image_adapters
    restore = lock_image_adapters()
    try:
        entry = ModelEntry(provider_id="sub2api", model_id="gpt-image-1", adapter_type="openai_images",
                           base_url="http://198.51.100.7/v1")
        with pytest.raises(adapters.AdapterError, match="only the offline mock"):
            adapters.adapter_for(entry).run(None)
        transport = adapters._TRANSPORT_FACTORY(entry)
        with pytest.raises(httpx.ConnectError):
            with httpx.Client(transport=transport) as client:
                client.get("http://198.51.100.7/v1/images")
        assert adapters.ADAPTERS["mock"].__name__ == "MockImageAdapter" and RefusedImageAdapter.attempts
    finally:
        restore()
    assert adapters.ADAPTERS["openai_images"].__name__ == "OpenAIImagesAdapter"


def test_the_network_guard_lets_only_the_text_api_through():
    import asyncio
    from tools.harness import NetworkGuard
    guard = NetworkGuard(["http://203.0.113.5:666/v1"]).install()
    try:
        guard._check(("203.0.113.5", 666))                                   # the text-model API: allowed
        for address in (("198.51.100.7", 443), ("127.0.0.1", 666), ("127.0.0.1", 9), ("203.0.113.5", 80)):
            with pytest.raises(ConnectionRefusedError):
                guard._check(address)
        with pytest.raises(ConnectionRefusedError):
            socket.socket().connect(("192.0.2.1", 80))                       # a real attempt, refused locally
        asyncio.run(asyncio.sleep(0))                                        # asyncio's own local pipes still work
        summary = guard.summary()
        assert summary["allowed_connections"] == 1 and "192.0.2.1:80" in summary["blocked_connections"]
        assert summary["local_pipes"] >= 1 and summary["allowed_endpoints"] == ["203.0.113.5:666"]
    finally:
        guard.uninstall()
    assert guard.summary()["installed"] is False


def test_a_mock_session_only_ever_allows_the_real_text_endpoint(clean_env, violetdb, tmp_path, monkeypatch):
    """--real --image-mock: the guard allows exactly the API url of the gate, nothing else."""
    import openai
    from tools.violet_test_chat import build_parser, open_session
    from tools.harness import MockLLM
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: MockLLM())
    clean_env.update({"VIOLET_TEST_ALLOW_REAL_API": "1", "VIOLET_TEST_API_KEY": "sk-test-b81-0123456789"})
    s = open_session(build_parser().parse_args(["--workdir", str(tmp_path / "violet-testchat-realtext"), "--image-mock",
                                                "--real", "--api-url", "http://203.0.113.5:666/v1", "--yes"]),
                     out=lambda x: None, db=violetdb)
    try:
        assert s.network_guard.summary()["allowed_endpoints"] == ["203.0.113.5:666"]
        with pytest.raises(ConnectionRefusedError):
            s.network_guard._check(("127.0.0.1", 666))                       # the default local relay of the provider file
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Through the CLI: rounds 21-22 offline, kept directories untouched
# ---------------------------------------------------------------------------

def test_rounds_21_and_22_through_the_cli(clean_env, violetdb, tmp_path, monkeypatch, script):
    from tools import dialogue_eval, violet_test_chat
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input, **kw: real_open(args, input_fn=input_fn, out=lambda s: None, db=violetdb, **kw))
    out = tmp_path / "out"
    rc = dialogue_eval.main(["--rounds", "21-22", "--image-mock", "--workdir", str(tmp_path / "violet-testchat-cli"),
                             "--output-dir", str(out)])
    assert rc == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["image_provider"] == "mock" and report["options"]["image_mock"] is True
    assert report["network_guard"]["blocked_connections"] == [] and report["network_guard"]["active_during_run"]
    r21, r22 = report["rounds"]
    assert r21["image_checks"]["valid"] and r21["image_checks"]["failures"] == {} and r21["auto"]["feature_off"] is None
    assert r22["image_checks"]["premise"]["met"] and r22["image_checks"]["results"] == {"tools_lacks:create_image": True}
    summary = dialogue_eval.summarize(report)
    assert summary["image_check_failures"] == [] and summary["image_rounds_invalid"] == []
    assert not (tmp_path / "violet-testchat-cli").exists()


def test_seeding_with_image_mock_leaves_the_seed_untouched(clean_env, violetdb, tmp_path, monkeypatch, script):
    import sqlite3
    from tools import dialogue_eval, violet_test_chat
    from tools.replay import load_plan
    meta = load_plan(PROJECT / "tests" / "samples" / "acceptance_dialogue_30.json")[1]
    seed = tmp_path / "violet-testchat-r20"
    seed.mkdir()
    state = {"version": 1, "plan": "x", "plan_sha256": meta["base_sha256"],
             "plan_revisions": {"2": {"file": "x", "sha256": meta["revision_sha256"], "changed_rounds": [15]}},
             "replay": None, "rounds": [{"n": n, "mode": "real", "plan_revision": 2} for n in range(1, 21)],
             "clock_end": "2026-10-22T16:08:08+08:00"}
    (seed / ".violet-eval-state.json").write_text(json.dumps(state), encoding="utf-8")
    src, dst = sqlite3.connect(violetdb.db_path), sqlite3.connect(seed / "violet-test.db")
    src.backup(dst)
    dst.close(), src.close()
    before = sorted((p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in seed.iterdir())
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input, **kw: real_open(args, input_fn=input_fn, out=lambda s: None, db=violetdb, **kw))
    rc = dialogue_eval.main(["--seed-from", str(seed), "--rounds", "21-22", "--image-mock",
                             "--workdir", str(tmp_path / "violet-testchat-r21"), "--output-dir", str(tmp_path / "o2")])
    assert rc == 0
    assert sorted((p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in seed.iterdir()) == before


def test_a_failed_round21_makes_round22_invalid_end_to_end(clean_env, violetdb, tmp_path, monkeypatch, script):
    from tools import dialogue_eval, violet_test_chat, harness
    original = harness.write_mock_image_config
    monkeypatch.setattr(violet_test_chat, "write_mock_image_config",
                        lambda workdir, options=None: original(workdir, {"fail": ["unknown_format"]}))
    real_open = violet_test_chat.open_session
    monkeypatch.setattr(violet_test_chat, "open_session",
                        lambda args, input_fn=input, **kw: real_open(args, input_fn=input_fn, out=lambda s: None, db=violetdb, **kw))
    out = tmp_path / "out-fail"
    assert dialogue_eval.main(["--rounds", "21-22", "--image-mock", "--workdir", str(tmp_path / "violet-testchat-fail"),
                               "--output-dir", str(out)]) == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    r21, r22 = report["rounds"]
    assert r21["image_checks"]["results"]["db:violet_picture_sent"] is False
    assert r22["image_checks"]["valid"] is False and r22["image_checks"]["premise"]["met"] is False
    summary = dialogue_eval.summarize(report)
    assert summary["image_rounds_invalid"] == [22]
    assert {f["check"] for f in summary["image_check_failures"]} >= {"db:violet_picture_sent", "premise"}


# ---------------------------------------------------------------------------
# Each layer on its own (they back each other up, so each is tested separately)
# ---------------------------------------------------------------------------

def test_the_raw_file_check_alone_refuses_a_provider_without_models(tmp_path, clean_env):
    """The product registry only sees models; a real provider block without models is caught by the raw check."""
    from tools.harness import IsolationError, validate_image_config, write_mock_image_config
    path = write_mock_image_config(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["providers"].append({"provider_id": "sub2api", "adapter_type": "openai_images", "models": []})
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(IsolationError, match="sub2api/\*: adapter_type"):
        validate_image_config(path)


def test_the_registry_check_alone_refuses_what_the_product_would_load(tmp_path, clean_env, monkeypatch):
    """If the product registry ever resolved an entry differently from the raw file, its view decides too."""
    from ai.images import registry as registry_module
    from tools.harness import IsolationError, validate_image_config, write_mock_image_config
    path = write_mock_image_config(tmp_path)
    original = registry_module.ImageProviderRegistry._load

    def load_with_a_real_entry(self):
        original(self)
        for entry in self.entries.values():
            entry.adapter_type = "openai_images"
    monkeypatch.setattr(registry_module.ImageProviderRegistry, "_load", load_with_a_real_entry)
    with pytest.raises(IsolationError, match="mock/mock-image-1: adapter_type 'openai_images'"):
        validate_image_config(path)


def _images(task_status="succeeded", sent_sha="abc", outbound_status="sent"):
    return {"provider": "mock",
            "tasks": [{"kind": "image_generate", "status": task_status, "output_asset_ids": [7], "error_code": None}],
            "calls": [{"entry_id": "mock/mock-image-1"}],
            "assets": [{"asset_id": 7, "direction": "outbound", "ai_generated": True, "status": "ready",
                        "sha256": "abc", "provider_id": "mock", "scene_kind": "current_thread"}],
            "outbound": [{"asset": "asset:7", "status": outbound_status}],
            "sent": [{"recorded": True, "sha256": sent_sha}]}


def test_the_three_facts_need_their_own_evidence():
    from tools.dialogue_eval import picture_facts
    chat = [{"agent": "ChatAgent", "tool_calls": ["create_image"]}]
    assert picture_facts(_images(), chat)["sent_recorded"] is True
    assert picture_facts(_images(sent_sha="other"), chat)["sent_recorded"] is False     # another file was recorded
    assert picture_facts(_images(outbound_status="failed"), chat)["sent_recorded"] is False
    failed = picture_facts(_images(task_status="failed"), chat)
    assert failed["generated"] is False and failed["sent_recorded"] is False          # an asset of a failed task
    assert picture_facts(_images(), [{"agent": "IntentAgent", "tool_calls": ["create_image"]}])["tool_called"] is False
