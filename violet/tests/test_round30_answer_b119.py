"""B.11.9: round-30 answer assertion and post-hoc offline reassessment; offline only, no model calls."""
import copy
import json
import os
import subprocess
import sys
import uuid

import pytest

from tools.synthetic_rounds import PROJECT, digest
from tools import synthetic_tail as tail
from tools import reassess_tail as reassess
from tests.test_selected_tail_b117 import BOOTSTRAP as SELECTED_BOOTSTRAP

REAL_REPLY = "记得呀，小满，你天文观测课程的答辩当时挺顺利的。"
REAL_RUN = PROJECT.parent / "local/b118/real-20261019-060349"
REAL_RUN_SHA256 = {
    "specialized_report.json": "4a5260152502662bd55a8347de832da6fc00f0c60e5e1c24f6b412d55fedc642",
    "eve-test.db": "609854e0bdb654a01fda58883b95fbeb8bbef47bfcc615cacfed946edad0394e",   # a 2026-10-19 file
    "seed.json": "40f9c3b2259cd6889542778188d32cc17a46a00fccb467cbe4cab23dd50774ff"}

POSITIVE = [REAL_REPLY,
    "记得，答辩已经顺利结束了。",
    "答辩结束了，挺顺利的",
    "记得！你答辩顺利通过啦",
    "当然没忘，天文观测的答辩那天挺顺利的。",
    "怎么会忘记呢，你的答辩很成功，已经过了呀",
    "记得记得，答辩顺利搞定了～",
    "记得，答辩那次很圆满。那天你是不是特别开心？",
    "嗯，我记得。答辩当时挺顺利的！后来有好好休息吗？"]

NEGATIVE = {
    "unfinished": ["答辩还没结束吧，准备得怎么样了？", "记得，你下周三答辩，会顺利的！", "答辩还没开始呢，别紧张",
                   "记得，答辩当时挺顺利的。啊不对，你还没答辩呢"],
    "preparing": ["你还在准备答辩吧，加油，一定会顺利的", "答辩准备中吗？之前说挺顺利的", "正在准备答辩，已经挺顺利了"],
    "forgot": ["不好意思，我不记得答辩结果了", "我忘了答辩怎么样了，顺利结束了吗？", "有点想不起来了，答辩当时顺利吗",
               "记不太清了，答辩那天挺顺利的"],
    "guess": ["答辩应该挺顺利的吧", "好像答辩已经顺利结束了？", "我猜答辩当时挺顺利的", "答辩大概已经顺利通过了",
              "答辩可能顺利结束了"],
    "denial": ["你好像没有答辩呀", "答辩当时不太顺利", "答辩没通过，别难过", "那次答辩失败了，没关系", "答辩挂了也没事"],
    "prediction": ["放心，答辩肯定顺利", "相信你答辩一定成功"],
    "no_outcome": ["记得呀", "记得，你说过答辩的事", "答辩已经结束了", "那天的答辩我记得"],
    "wish_or_question": ["祝你答辩顺利！", "答辩顺利吗？", "希望答辩当时顺利结束了"]}


@pytest.mark.parametrize("text", POSITIVE)
def test_natural_completed_outcome_is_recalled(text):
    assert tail.recalls_completed_outcome(text)


@pytest.mark.parametrize("text", [t for group in NEGATIVE.values() for t in group])
def test_unfinished_forgotten_guessed_denied_or_missing_outcome_is_rejected(text):
    assert not tail.recalls_completed_outcome(text)


def test_real_reply_was_rejected_only_by_v1_wording_rule():
    assert not reassess.answer_v1(REAL_REPLY)  # Root cause: no word from 结束|完成|通过|过了|搞定.
    assert tail.recalls_completed_outcome(REAL_REPLY)
    assert not tail.re.search(tail.UNFINISHED, REAL_REPLY)


TARGET = {"id": 1, "content": "用户已完成天文观测课程答辩，过程挺顺利。"}
GOOD = dict(complete=True, calls=[{"agent": "IntentAgent", "tool_calls": ["query_memory"]}],
            recalled=[{**TARGET, "progress": "done"}], chat_lines=[f"- id=1; [事件 / 已结束] {TARGET['content']}"],
            target=TARGET)


def test_structural_checks_stay_strict_even_with_a_correct_answer():
    assert all(tail.round30_checks(REAL_REPLY, **GOOD).values())
    broken = {"no_query": {"calls": [{"agent": "IntentAgent", "tool_calls": []}]},
        "no_recall": {"recalled": []},
        "wrong_target": {"recalled": [{"id": 2, "content": "泰山", "progress": "done"}]},
        "not_done": {"recalled": [{**TARGET, "progress": "planned"}]},
        "absent_from_chat": {"chat_lines": []},
        "chat_not_finished": {"chat_lines": [f"- id=1; [事件] {TARGET['content']}"]},
        "incomplete": {"complete": False}}
    for name, change in broken.items():
        checks = tail.round30_checks(REAL_REPLY, **{**GOOD, **change})
        assert checks["answer_recalls_outcome"] and not all(checks.values()), name


BOOTSTRAP = SELECTED_BOOTSTRAP.replace("requests.append(agent)", '''requests.append(agent)
        if agent=="ChatAgent" and "答辩你还记得" in user and options.get("chat_reply"):
            return self._completion(options["chat_reply"],kwargs["model"])''', 1)


def run_case(*flags, **options):
    directory = PROJECT.parent / "local/b119/test-cases" / uuid.uuid4().hex
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", BOOTSTRAP, json.dumps({
        "argv": ["--suite", tail.PURPOSE, "--rounds", "30-32", "--workdir", str(directory), *flags], **options})],
        cwd=PROJECT, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads((directory / "specialized_report.json").read_text(encoding="utf-8"))


def test_real_reply_text_passes_full_offline_suite_and_31_32_still_pass():
    report = run_case("--real", chat_reply=REAL_REPLY)
    assert report["verification_environment"] == "fake_sdk_offline" and report["status"] == "通过"
    r30 = report["rounds"][0]
    assert r30["violet"] == [REAL_REPLY] and r30["answer_assertion"] == tail.ANSWER_ASSERTION
    assert all(r30["checks"].values()) and list(r30["checks"]) == [
        "execution_complete", "intent_queried", "correct_target_done", "correct_event_in_chat",
        "answer_recalls_outcome", "no_unfinished_claim"]
    assert [r["status"] for r in report["rounds"]] == ["通过", "通过", "通过"]


@pytest.mark.parametrize("fault", ["wrong_target", "no_query"])
def test_correct_wording_without_actual_recall_fails_round30(fault):
    report = run_case("--real", fault=fault, chat_reply=REAL_REPLY)
    r30 = report["rounds"][0]
    assert r30["checks"]["answer_recalls_outcome"] and r30["status"] == "失败"
    assert not (r30["checks"].get("correct_target_done") and r30["checks"].get("intent_queried"))
    assert [r["status"] for r in report["rounds"]] == ["失败", "未执行", "未执行"]


@pytest.mark.parametrize("reply", ["答辩应该挺顺利的吧", "我不记得答辩结果了", "你还在准备答辩吧，加油"])
def test_wrong_answer_fails_full_offline_suite(reply):
    report = run_case("--real", chat_reply=reply)
    assert [r["status"] for r in report["rounds"]] == ["失败", "未执行", "未执行"]


needs_real_run = pytest.mark.skipif(not (REAL_RUN / "specialized_report.json").is_file(),
                                    reason="local real-run evidence is not in this checkout")


@needs_real_run
def test_reassessment_of_recorded_real_run_changes_only_the_answer_check():
    value, hashes = reassess.reassess(REAL_RUN)
    assert {k: hashes[k] for k in REAL_RUN_SHA256} == REAL_RUN_SHA256
    assert value["kind"] == "post_hoc_offline_reassessment" and value["model_calls"] == 0
    assert set(value["violet_head"]) == {"head", "tools_uncommitted"}
    assert not value["real_model_reexecuted"] and value["source"]["status"] == "失败"
    r30 = value["round30"]
    assert r30["evidence_problems"] == [] and r30["status"] == "通过"
    assert r30["recomputed_checks_v1"] == r30["recorded_checks"]
    assert r30["differences"] == [{"check": "answer_recalls_outcome", "recorded": False, "v1": False, "v2": True}]
    assert [(r["n"], r["status"], r["reassessed"]) for r in value["rounds_31_32"]] == [(31, "未执行", False), (32, "未执行", False)]
    assert value["original_scores_unchanged"]["original_32_score"] is None
    assert "31–32未执行" in value["overall"]
    assert all(digest(REAL_RUN / k) == v for k, v in REAL_RUN_SHA256.items())


def _variant(tmp_path, change):
    source = json.loads((REAL_RUN / "specialized_report.json").read_text(encoding="utf-8"))
    report = copy.deepcopy(source)
    change(report, next(r for r in report["rounds"] if r["n"] == 30))
    (tmp_path / "specialized_report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return reassess.reassess(tmp_path)[0]["round30"]


@needs_real_run
@pytest.mark.parametrize("name,change,status", [
    ("mock_run", lambda rep, r: rep.update(real=False), "无法验证"),
    ("missing_recall_evidence", lambda rep, r: r.pop("recall_evidence"), "无法验证"),
    ("missing_chat_audit", lambda rep, r: rep.update(chat_context_audit=[]), "无法验证"),
    ("failed_call", lambda rep, r: r["calls"][0].update(ok=False), "无法验证"),
    ("no_reply", lambda rep, r: r.update(violet=[]), "无法验证"),
    ("returned_not_done", lambda rep, r: r["recall_evidence"]["returned"][0].update(progress="planned"), "无法验证"),
    ("chat_line_missing", lambda rep, r: r["recall_evidence"].update(chat_event_lines=[]), "无法验证"),
    ("other_recorded_failure", lambda rep, r: r["checks"].update(correct_event_in_chat=False), "无法验证"),
    ("forgot_reply", lambda rep, r: r.update(violet=["我不记得答辩结果了"]), "失败"),
    ("guessed_reply", lambda rep, r: r.update(violet=["答辩应该挺顺利的吧"]), "失败")])
def test_reassessment_never_passes_without_complete_consistent_evidence(tmp_path, name, change, status):
    assert _variant(tmp_path, change)["status"] == status, name


@needs_real_run
def test_reassessment_cli_writes_only_a_new_report():
    out = PROJECT.parent / "local/b119/test-cases" / uuid.uuid4().hex
    command = [sys.executable, "-B", "-X", "utf8", "-m", "tools.reassess_tail", "--source", str(REAL_RUN), "--out", str(out)]
    run = lambda: subprocess.run(command, cwd=PROJECT, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run().returncode == 0
    value = json.loads((out / "round30_reassessment.json").read_text(encoding="utf-8"))
    assert value["source_unchanged"] and value["round30"]["status"] == "通过"
    assert run().returncode != 0  # Existing output is never overwritten.
    assert all(digest(REAL_RUN / k) == v for k, v in REAL_RUN_SHA256.items())
