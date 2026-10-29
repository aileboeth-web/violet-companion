"""Offline test harness for Violet.

Everything here runs BEFORE any project module is imported:
  * the process is pointed at a throw-away .env / SQLite file in a temp dir,
  * all credentials are obviously fake,
  * a socket guard makes any non-loopback connection fail loudly,
so no test can reach WeChat, the LLM relay, MCP relays or a production DB.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="violet-tests-"))

os.environ.update({
    "VIOLET_ENV_FILE": str(_TMP / "test.env"),          # intentionally missing
    "VIOLET_DB_PATH": str(_TMP / "test-memories.db"),
    "VIOLET_DATA_DIR": str(_TMP / "data"),                # never create ./data in the project
    "LLM_API_KEY": "sk-test-not-a-real-key",
    "LLM_API_URL": "http://127.0.0.1:9/v1",           # discard port; never contacted
    "MCP_SERVERS": "",
    "DEBUG_MODE": "0",
    "VIOLET_UPDATE_CHECK": "0",                    # never ask GitHub; update tests pass a fake fetcher
    "INTENT_LLM_MODEL": "fake-intent",
    "CHAT_LLM_MODEL": "fake-chat",
    "MEMORY_LLM_MODEL": "fake-memory",
    "PERFORMER_LLM_MODEL": "fake-performer",
    "PROACTIVE_LLM_MODEL": "fake-proactive",
    "LLM_RETRY_BASE_DELAY": "0",
    "VIOLET_ADMIN_TOKEN": "test-admin-token-0123456789",
    # the scheduler would advance Violet's life on wall-clock time; phase-4 tests enable it explicitly
    "VIOLET_LIFE_ENGINE": "0",
    # phase 8.9: Violet's ordinary day depends on the date; tests that need it enable it with a fixed clock
    "VIOLET_DAILY_LIFE": "0",
    # web access is off unless a test turns it on; no real search backend is ever configured
    "VIOLET_WEB_ENABLED": "0",
    "VIOLET_WEB_SEARCH_PROVIDER": "none",
    "VIOLET_WEB_EXPLORER": "0",
    # image system off unless a test turns it on; media files go to the temp dir
    "VIOLET_IMAGE_ENABLED": "0",
    "VIOLET_MEDIA_DIR": str(_TMP / "media"),
})
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_key, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


# ---------------------------------------------------------------------------
# Network guard: only loopback connections are allowed.
# ---------------------------------------------------------------------------

class NetworkBlocked(RuntimeError):
    pass


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        host = address[0]
    else:
        return True  # AF_UNIX etc.
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guarded_connect(self, address):
    if not _is_loopback(address):
        raise NetworkBlocked(f"test attempted non-loopback connection to {address!r}")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _is_loopback(address):
        raise NetworkBlocked(f"test attempted non-loopback connection to {address!r}")
    return _real_connect_ex(self, address)


_real_getaddrinfo = socket.getaddrinfo


def _guarded_getaddrinfo(host, *args, **kwargs):
    # Block DNS lookups for anything that is not local, so not even a resolver
    # query for a WeChat / LLM / MCP hostname leaves the machine.
    if host is not None:
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name not in ("localhost",):
            try:
                if not ipaddress.ip_address(name).is_loopback:
                    raise NetworkBlocked(f"test attempted to resolve non-loopback address {name!r}")
            except ValueError:
                raise NetworkBlocked(f"test attempted DNS lookup of {name!r}") from None
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TABLES = (
    "messages", "task_sessions", "event_memories", "core_memories",
    "proactive_events", "proactive_settings", "inbound_messages", "outbound_messages",
    "memory_revisions", "commitments", "topic_mentions", "memory_jobs",
    "user_profile_facts", "user_situations", "interaction_preferences", "preference_observations",
    "profile_questions", "profile_audit_log",
    "persona_core_versions", "persona_dynamic", "life_threads", "life_plans", "life_events", "life_revisions",
    "life_engine_runs", "consistency_flags", "aux_llm_calls", "life_settings",
    "web_findings", "web_sources", "web_requests",
    "media_assets", "image_tasks", "image_calls", "image_model_settings", "image_capability_checks",
    "visual_profile_versions",
    # phase 9.4: children first (foreign keys are on)
    "schedule_revisions", "schedule_exceptions", "schedule_exams", "schedule_holidays", "schedule_slots",
    "schedule_courses", "schedule_periods", "schedule_imports", "schedule_terms",
    # phase 10: the owner's bound chat accounts
    "channel_pairing_codes", "channel_identities",
    # 0.7.9: stickers, pictures in social messages, QQ 空间 posts
    "stickers", "social_media", "social_posts",
)


@pytest.fixture()
def violetdb():
    """The process-wide VioletDatabase, emptied before each test."""
    from ai.chat import violetdb as db
    for table in _TABLES:
        db.execute(f"DELETE FROM {table}")
    db.execute("UPDATE message_stats SET value = 0")
    yield db


@pytest.fixture()
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_mcp_registry():
    """MCP health/caches are process-wide; isolate tests from each other."""
    yield
    try:
        from ai import mcp as mcp_module
        reset = getattr(mcp_module, "reset_registry", None)
        if reset:
            reset()
    except Exception:
        pass
    try:
        from ai.web.service import clear_search_cache      # phase 9.3: the search cache is process-wide too
        clear_search_cache()
    except Exception:
        pass


def pytest_sessionfinish(session, exitstatus):
    print(f"\n[violet-tests] isolated temp dir: {_TMP}")
