"""2026-11-02 the 流程 page: ai/pipeline_state.py keeps the last 20 runs, one per thread, and the control routes."""

import threading

from ai import pipeline_state as ps


def setup_function():
    ps.clear()


def test_runs_are_kept_per_thread_and_limited():
    ps.pipeline_start("chat", "你好", turn_id="t-1")
    ps.node_skip("Intent", "关闭")
    ps.node_start("Chat", {"user_message": "你好"})

    def social():
        ps.pipeline_start("social", "群友：在吗", channel="qq", title="群 1")
        ps.node_start("Social", {"messages": [{"role": "system", "content": "x"}]})
        ps.node_done("Social", "Act", {"decision": {"action": "silent"}})
        ps.pipeline_end()
    t = threading.Thread(target=social)
    t.start()
    t.join()
    # the group decided in its own thread: your own turn is still the current one here
    ps.node_done("Chat", "Memory", {"content": "嗨", "messages": [{"role": "user", "content": "你好"}], "error": None})
    ps.node_skip("Memory", "后台整理")
    ps.pipeline_end()
    runs = ps.recent()
    assert [r["pipeline_type"] for r in runs] == ["social", "chat"]          # newest started first
    chat = ps.get_run(runs[1]["id"])
    assert chat["turn_id"] == "t-1" and chat["running"] is False
    assert chat["nodes"]["Intent"]["status"] == "skipped" and chat["nodes"]["Intent"]["note"] == "关闭"
    assert chat["nodes"]["Chat"]["status"] == "done" and chat["nodes"]["Chat"]["output"]["content"] == "嗨"
    assert chat["nodes"]["Performer"]["status"] == "skipped"          # never reached: skipped at the end
    assert runs[0]["nodes"]["Act"]["status"] == "skipped" and runs[0]["channel"] == "qq"
    for i in range(ps.RUN_LIMIT + 5):
        ps.pipeline_start("proactive", str(i))
        ps.pipeline_end()
    assert len(ps.recent()) == ps.RUN_LIMIT


def test_failed_node_and_calls_without_a_run():
    ps.node_start("Chat", {"a": 1})                    # no run in this thread: ignored, no error
    ps.pipeline_end()
    assert ps.recent() == []
    ps.pipeline_start("chat", "x")
    ps.node_start("Chat", "x" * (ps.TEXT_LIMIT + 10))
    ps.node_done("Chat", output_data={"error": "TimeoutError: slow"})
    ps.pipeline_end(error="chat: TimeoutError")
    run = ps.get_run(ps.recent()[0]["id"])
    assert run["nodes"]["Chat"]["status"] == "failed" and run["error"] == "chat: TimeoutError"
    assert len(run["nodes"]["Chat"]["input"]) < ps.TEXT_LIMIT + 40      # clipped
    assert ps.get_state()["pipeline_type"] == "chat"                      # the old /api/pipeline-status shape


def test_clip_makes_plain_json():
    class Thing:
        def __str__(self):
            return "thing"
    out = ps.clip({"a": (1, 2), "b": {3}, "c": Thing(), "d": list(range(ps.ITEM_LIMIT + 3))})
    assert out["a"] == [1, 2] and out["b"] == [3] and out["c"] == "thing"
    assert len(out["d"]) == ps.ITEM_LIMIT + 1


def test_flow_routes(tmp_path, monkeypatch):
    from flask import Flask
    from wx_ilink_py.control_routes import register_control_routes
    app = Flask(__name__)
    register_control_routes(app, violetdb=None)
    ps.pipeline_start("chat", "你好")
    ps.node_start("Chat", {"user_message": "你好"})
    c = app.test_client()
    runs = c.get("/api/control/flow").get_json()["data"]["runs"]
    assert runs[0]["running"] is True and runs[0]["active_node"] == "Chat"
    assert "input" not in runs[0]["nodes"]["Chat"]                       # the list has no contents
    one = c.get(f"/api/control/flow/{runs[0]['id']}").get_json()["data"]
    assert one["nodes"]["Chat"]["input"] == {"user_message": "你好"}
    assert c.get("/api/control/flow/999999").status_code == 404
    ps.pipeline_end()
