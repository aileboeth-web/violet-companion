"""The two observed B117 queries, lexical precision and the actual Chat pipeline."""
import json
import os
import subprocess
import sys
import uuid

import pytest

from tests.fakes import FakeLLM, default_responders, install_fake_llm, intent_json
from tests.test_phase2_scenarios import make_world, say, script, tool

INTENT_QUERY = "答辩 上次 之前提到 答辩准备 时间 内容"
MEMORY_QUERY = "用户之前提到的答辩、毕业答辩或论文答辩相关事情，计划、时间、准备情况"
CONTENT = "用户已完成天文观测课程答辩，过程挺顺利。"


@pytest.fixture
def agents(monkeypatch, violetdb):
    install_fake_llm(monkeypatch, FakeLLM(default_responders()))
    from ai.agents import IntentAgent, MemoryAgent
    return {"intent": IntentAgent(), "memory": MemoryAgent()}


@pytest.mark.parametrize("name,query,threshold", [("intent", INTENT_QUERY, .25), ("memory", MEMORY_QUERY, .20)])
def test_observed_full_query_recalls_completed_target(agents, violetdb, name, query, threshold):
    target = violetdb.add_event_memory(CONTENT, category="event", kind="event", progress="done")
    violetdb.add_event_memory("用户去爬了泰山，感到很累。", category="event", kind="event", progress="done")
    found = agents[name]._tool_query_memory({"query": query, "limit": 5, "min_similarity": threshold})["memories"]
    assert [(r["id"], r["content"], r["progress"]) for r in found] == [(target, CONTENT, "done")]


@pytest.mark.parametrize("query", [INTENT_QUERY, MEMORY_QUERY])
def test_each_real_query_enters_chat_as_actual_done_event(make_world, query):
    w = make_world()
    target = w.db.add_event_memory(CONTENT, category="event", kind="event", progress="done")
    w.llm.responders["fake-intent"] = script(tool("query_memory", query=query, min_similarity=.25), final=intent_json())
    w.llm.responders["fake-memory"] = script(tool("query_memory", query=MEMORY_QUERY, min_similarity=.20))
    seen = []
    def chat(request):
        system = "\n".join(m.get("content", "") for m in request["messages"] if m["role"] == "system")
        lines = [l for l in system.splitlines() if l.startswith(f"- id={target}; ")]
        assert any(CONTENT in line and "已结束" in line for line in lines)
        seen.extend(lines)
        return "记得，答辩已经顺利结束了。"
    w.llm.responders["fake-chat"] = chat
    say(w, "上次说的答辩你还记得吗")
    assert seen
    request = w.llm.calls_for("fake-memory")[-1]
    results = [json.loads(m["content"])["memories"] for m in request["messages"] if m["role"] == "tool"]
    assert any(any(m["id"] == target and m["progress"] == "done" for m in rows) for rows in results)


@pytest.mark.parametrize("query,content", [
    ("吉他", "用户的吉他演出已经结束，反响不错。"),
    ("你还记得上次说的吉他演出吗", "用户的吉他演出已经结束，反响不错。"),
    ("服务器故障 上次 之前提到 生产环境 凌晨 时间 内容", "用户的服务器故障已经修好了。"),
    ("吉他演出、音乐节吉他演出或校园吉他演出相关事情，计划、时间、准备情况", "用户的吉他演出已结束。"),
    ("研究生钢琴演出或毕业钢琴演出相关事情", "用户已完成学校钢琴演出。"),
    ("课程答辩 时间 内容", CONTENT),
    ("课程答辩", "用户的课程 答辩已经结束。"),
])
def test_other_topics_natural_questions_and_speculative_qualifiers(agents, violetdb, query, content):
    target = violetdb.add_event_memory(content, kind="event", progress="done")
    for name in agents:
        result = agents[name]._tool_query_memory({"query": query})["memories"]
        assert [r["id"] for r in result] == [target]
        assert result[0]["retrieval"]["topic_similarity"] >= .8


@pytest.mark.parametrize("query,wrong", [
    ("课程答辩", "用户的物理课程作业已经交了。"),
    ("课程答辩 时间 内容", "用户将参加课程考试。"),
    (INTENT_QUERY, "用户的准备时间和课程内容已经确定了。"),
    (MEMORY_QUERY, "用户的论文选题计划已经完成。"),
    ("物理", "用户给文件取名为理物。"),
    ("课程计划", "用户今天参加了另一门课程。"),
    ("用户之前提到的吉他演出", "用户之前提到了一次舞蹈比赛。"),
    ("服务器故障", "用户的打印机故障已经处理了。"),
    ("口头汇报", CONTENT),  # Semantic alias without a literal anchor is outside this algorithm.
    ("周一计划或周二计划", "用户的野营计划安排好了。"),
])
def test_unrelated_or_prefix_only_memories_are_not_recalled(agents, violetdb, query, wrong):
    violetdb.add_event_memory(wrong, kind="event", progress="done")
    for agent in agents.values():
        assert agent._tool_query_memory({"query": query})["memories"] == []


def test_similar_events_are_distinguished_and_progress_is_not_inferred(agents, violetdb):
    done = violetdb.add_event_memory(CONTENT, kind="event", progress="done")
    pending = violetdb.add_event_memory("用户下个月参加天文观测课程答辩。", kind="event", progress="planned")
    other = violetdb.add_event_memory("用户正在准备毕业论文答辩。", kind="event", progress="ongoing", importance=99)
    course = violetdb.add_event_memory("用户的天文课程考试结束了。", kind="event", progress="done", importance=99)
    rows = agents["intent"]._tool_query_memory({"query": "课程答辩 时间 内容"})["memories"]
    assert {r["id"]: r["progress"] for r in rows} == {done: "done", pending: "planned"}
    assert not ({other, course} & {r["id"] for r in rows})
    rows = agents["intent"]._tool_query_memory({"query": INTENT_QUERY})["memories"]
    assert {r["id"] for r in rows} == {done, pending, other}
    assert violetdb.get_event_memory(pending)["progress"] == "planned"


def test_threshold_category_limit_and_legacy_write_metric_preserved(agents, violetdb):
    target = violetdb.add_event_memory(CONTENT, category="event", kind="event", progress="done")
    violetdb.add_event_memory(CONTENT, category="other", kind="event", progress="done", importance=99)
    assert violetdb.search_event_memories(INTENT_QUERY, category="event") == []  # Legacy/dedup mode unchanged.
    assert violetdb.text_similarity(INTENT_QUERY, CONTENT) == pytest.approx(.11428571428571428)
    rows = agents["memory"]._tool_query_memory({"query": MEMORY_QUERY, "category": "event", "limit": 1, "min_similarity": .20})["memories"]
    assert [r["id"] for r in rows] == [target]
    assert agents["memory"]._tool_query_memory({"query": MEMORY_QUERY, "min_similarity": .99})["memories"] == []
    assert agents["intent"]._tool_query_memory({"query": INTENT_QUERY, "category": "missing"})["memories"] == []


def test_disabled_deleted_and_expired_cannot_gain_context_privileges(agents, violetdb):
    from ai.context_engine import ContextEngine
    disabled = violetdb.add_event_memory(CONTENT, kind="event", progress="done")
    deleted = violetdb.add_event_memory(CONTENT, kind="event", progress="done")
    expired = violetdb.add_event_memory(CONTENT, kind="event", progress="done")
    violetdb.set_event_memory_enabled(disabled, False)
    violetdb.execute("UPDATE event_memories SET status='deleted' WHERE id=?", (deleted,))
    violetdb.execute("UPDATE event_memories SET valid_until='2000-01-01T00:00:00+08:00' WHERE id=?", (expired,))
    intent = agents["intent"]._tool_query_memory({"query": INTENT_QUERY})["memories"]
    assert [r["id"] for r in intent] == [expired] and intent[0]["status"] == "expired"
    memory = agents["memory"]._tool_query_memory({"query": MEMORY_QUERY, "min_similarity": .2})["memories"]
    assert {r["id"] for r in memory} == {disabled, expired}  # Internal bookkeeping may inspect disabled rows.
    text = ContextEngine(violetdb)._recall_section(memory).render()
    assert f"id={disabled};" not in text and f"id={deleted};" not in text
    assert f"id={expired};" in text and "已过有效期" in text


def test_hidden_primary_variant_cannot_block_visible_shared_topic(agents, violetdb):
    hidden = violetdb.add_event_memory("用户的红色吉他演出已经结束。", kind="event", progress="done")
    visible = violetdb.add_event_memory("用户已完成学校吉他演出。", kind="event", progress="done")
    violetdb.set_event_memory_enabled(hidden, False)
    found = agents["intent"]._tool_query_memory({"query": "红色吉他演出或蓝色吉他演出相关事情"})["memories"]
    assert [r["id"] for r in found] == [visible]


def test_specific_primary_is_not_overridden_by_speculative_secondary(agents, violetdb):
    course = violetdb.add_event_memory(CONTENT, kind="event", progress="done")
    graduate = violetdb.add_event_memory("用户的毕业论文答辩下周举行。", kind="event", progress="planned")
    rows = agents["intent"]._tool_query_memory({"query": "课程答辩 或 毕业论文答辩 时间 内容"})["memories"]
    assert [r["id"] for r in rows] == [course] and graduate not in [r["id"] for r in rows]


@pytest.mark.parametrize("threshold", [0, -.5])
def test_low_model_threshold_cannot_dump_unrelated_events(agents, violetdb, threshold):
    violetdb.add_event_memory("用户的物理课程考试已经结束。", kind="event", progress="done")
    assert agents["memory"]._tool_query_memory({"query": "课程答辩", "min_similarity": threshold})["memories"] == []


def test_real_parameter_replay_in_fresh_selected_suite_with_actual_id1_and_audit():
    from tools.synthetic_rounds import PROJECT
    from tests.test_selected_tail_b117 import BOOTSTRAP
    code = BOOTSTRAP.replace('{"query":user,"category":"event","limit":2,"min_similarity":.3}',
        'options.get("memory_parameters",{"query":user,"category":"event","limit":2,"min_similarity":.3})')
    directory = PROJECT.parent / "local/b118/test-cases" / uuid.uuid4().hex
    options = {"argv": ["--suite", "round29_32_specialized_test", "--rounds", "30-32", "--real", "--workdir", str(directory)],
        "query_parameters": {"query": INTENT_QUERY, "limit": 5, "min_similarity": .25},
        "memory_query": True, "memory_parameters": {"query": MEMORY_QUERY, "limit": 5, "min_similarity": .20}}
    result = subprocess.run([sys.executable,"-B","-X","utf8","-c",code,json.dumps(options)],cwd=PROJECT,
        env=dict(os.environ,PYTHONIOENCODING="utf-8"),capture_output=True,text=True,encoding="utf-8",timeout=60)
    assert result.returncode == 0, (result.stdout,result.stderr)
    secret = b"fake-b115-private-test-credential-0000"
    assert secret not in (result.stdout+result.stderr).encode()
    assert all(secret not in p.read_bytes() for p in directory.rglob("*") if p.is_file())
    report = json.loads((directory / "specialized_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "通过" and report["verification_environment"] == "fake_sdk_offline"
    assert report["executed_rounds"] == [30,31,32] and not any(c["round"] == 29 for c in report["calls"])
    for name, expected, threshold in [("IntentAgent", options["query_parameters"], .35),
                                      ("MemoryAgent", options["memory_parameters"], .20)]:
        entry = next(a for a in report["memory_query_audit"] if a["agent"] == name)
        assert entry["parameters"] == expected and entry["searches"][0]["effective_parameters"]["min_similarity"] == threshold
        assert entry["returned_count"] == 1 and entry["returned_events"][0]["id"] == 1
        assert entry["returned_events"][0]["progress"] == "done"
        evidence = entry["searches"][0]["returned_events"][0]["retrieval"]
        assert evidence["original_similarity"] == pytest.approx(.11428571428571428 if name == "IntentAgent" else .16842105263157894)
        assert evidence["topic_similarity"] == evidence["similarity"] == .8
    chat = next(a for a in report["chat_context_audit"] if a["round"] == 30)
    assert chat["target_event_id"] == 1 and chat["target_event_in_context"]
    assert any(line.startswith("- id=1; ") and CONTENT in line and "已结束" in line for line in chat["event_lines"])
    assert all(report["rounds"][i]["status"] == "通过" for i in (1,2))  # Original correction criteria unchanged.
