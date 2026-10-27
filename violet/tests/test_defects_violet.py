"""Violet stage 1: the four defects found in the 2026-10-26 review, each pinned by a test (offline)."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from tests.fakes import text_message
from tests.test_runtime import env  # noqa: F401  (fixture)

PROJECT = Path(__file__).resolve().parents[1]


# 1 -- Python 3.10 was accepted, but ai/mcp.py uses the 3.11 builtin BaseExceptionGroup ---------------------------

def test_python_minimum_is_3_11_in_the_installer_and_the_bootstrap_script():
    from tools import violet_installer as inst
    assert "BaseExceptionGroup" in (PROJECT / "ai" / "mcp.py").read_text(encoding="utf-8")   # why 3.11 is needed
    assert inst.MIN_PYTHON >= (3, 11)
    script = (PROJECT / "install.sh").read_text(encoding="utf-8")
    candidates = re.search(r'for candidate in "\$\{PYTHON:-\}"(.*?); do', script).group(1).split()
    assert "python3.10" not in candidates and "python3.11" in candidates
    assert "sys.version_info >= (3, 11)" in script and "(3, 10)" not in script
    assert "python3.11 python3.11-venv" in script            # an old system python3 (Ubuntu 22.04) gets 3.11


# 2 -- the WeChat client appended every message to an in-memory queue that nothing ever read ----------------------

def test_wechat_client_keeps_no_copy_of_the_messages_it_fetched(tmp_path, monkeypatch):
    from wx_ilink_py.client import ILinkClient
    client = ILinkClient(data_dir=tmp_path / "d", auto_login=False)
    monkeypatch.setattr(client, "ensure_login", lambda: SimpleNamespace(base_url="http://127.0.0.1:9", token="t"))
    batch = [{"message_id": i, "from_user_id": "owner", "item_list": [{"type": 1, "text_item": {"text": "x" * 100}}]}
             for i in range(50)]
    monkeypatch.setattr(client, "_api_post", lambda *a, **k: {"ret": 0, "msgs": batch, "get_updates_buf": "b"})
    for _ in range(3):
        messages, _ = client.fetch_messages(timeout=1, persist_buf=False)
        assert len(messages) == 50                          # the caller (the runtime) gets every message
    assert not hasattr(client, "_queue") and not hasattr(client, "get_pending_messages")
    assert client.get_status()["queue_size"] == 0


# 3 -- WeChat messages were taken as the owner's without checking the sender ------------------------------------

def test_a_wechat_message_from_another_account_is_not_the_owners_words(env):  # noqa: F811
    client, db = env["client"], env["db"]
    client.credentials = SimpleNamespace(user_id="owner@im.wechat")
    rt = env["build"]()
    stranger = text_message("/bind 123456", message_id=1)
    stranger["from_user_id"] = "someone-else@im.wechat"
    client.queue_messages([stranger])
    assert rt.poll_once() == 0
    row = db.fetch_one("SELECT * FROM inbound_messages")
    assert row["status"] == "skipped" and "内容没有保存" in row["content"] and row["peer_id"] == "someone-else@im.wechat"
    assert db.fetch_all("SELECT * FROM messages") == []                       # nothing stored as the owner's words
    assert client.sent_texts == []                                           # no pairing answer (it would reach the owner)
    identity = db.fetch_one("SELECT * FROM channel_identities WHERE peer_id = 'someone-else@im.wechat'")
    assert identity["status"] == "pending"                                   # listed for the admin, never auto-bound
    assert rt.stats.get("wechat_unexpected_sender") == 1


def test_the_owners_own_messages_and_an_approved_sender_still_get_through(env):  # noqa: F811
    from ai.channels.identity import IdentityStore
    client, db = env["client"], env["db"]
    client.credentials = SimpleNamespace(user_id="owner@im.wechat")
    rt = env["build"]()
    mine = text_message("在吗", message_id=2)
    mine["from_user_id"] = "owner@im.wechat"
    unknown = text_message("没有发送者字段", message_id=3)                    # no sender id: not judged
    client.queue_messages([mine, unknown])
    assert rt.poll_once() == 2
    other = text_message("我换了个号", message_id=4)
    other["from_user_id"] = "second@im.wechat"
    client.queue_messages([other])
    assert rt.poll_once() == 0
    row = db.fetch_one("SELECT id FROM channel_identities WHERE peer_id = 'second@im.wechat'")
    IdentityStore(db).set_status(row["id"], "bound")                         # the admin approves it
    again = text_message("现在呢", message_id=5)
    again["from_user_id"] = "second@im.wechat"
    client.queue_messages([again])
    assert rt.poll_once() == 1


# 4 -- the 03:00 memory decay ran on the server's clock (often UTC), not on VIOLET_TIMEZONE ------------------------

def test_daily_jobs_wait_for_the_wall_clock_of_the_configured_zone():
    from ai.settings import seconds_until_daily
    shanghai = ZoneInfo("Asia/Shanghai")
    assert seconds_until_daily(3, now=datetime(2026, 10, 27, 2, 0, tzinfo=shanghai)) == 3600
    assert seconds_until_daily(3, now=datetime(2026, 10, 27, 3, 0, tzinfo=shanghai)) == 24 * 3600   # just ran
    # across a daylight-saving change: 23:00 EDT -> 03:00 EST next day is 5 real hours (4 on the wall clock)
    new_york = ZoneInfo("America/New_York")
    assert seconds_until_daily(3, now=datetime(2026, 10, 31, 23, 0, tzinfo=new_york)) == 5 * 3600


def test_the_decay_scheduler_uses_the_configured_zone():
    source = (PROJECT / "main.py").read_text(encoding="utf-8")
    loop = source.split("def _start_daily_decay_scheduler", 1)[1].split("\ndef ", 1)[0]
    assert "seconds_until_daily(3)" in loop and "datetime.now()" not in loop
