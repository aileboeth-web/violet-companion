"""Isolated Violet 2.0 test session (phase 7 B.1 / B.2).

Runs the REAL Violet pipeline (VioletRuntime -> IntentAgent / ChatAgent / MemoryAgent,
ContextEngine, ProactiveScheduler.tick on demand, MemoryWorker, outbox) with:

  * a throw-away directory for the database, data, media and logs
    (VIOLET_DB_PATH / VIOLET_DATA_DIR / VIOLET_MEDIA_DIR); production paths are refused;
  * VIOLET_ENV_FILE pointing to an EMPTY file in that directory: Violet re-reads its
    .env with override=True on every model call, so no real .env may ever be
    behind that path;
  * no WeChat (in-memory client), no proactive / life / explorer threads, no
    MCP servers, no web tools, no images, no dashboard;
  * --image-mock (phase 7 B.8.1): images on, but ONLY the offline mock image adapter:
    a mock-only provider file in the test directory (validated), every other adapter
    replaced by a refusing stub, HTTP transport refused, and a socket guard that lets
    through nothing but the real text-model endpoint (and asyncio's own local pipes);
    "sent" pictures are copied into <workdir>/sent_images, never to WeChat;
  * a mock model by default; a real model only through RealApiGate (explicit
    switch + explicit key + confirmation) and always behind BudgetedClient.

prepare_environment() must run BEFORE anything from `ai` is imported -- the
command line tools call it first thing.

Call details (phase 7 B.2) are built from an explicit allow-list of numeric /
code fields: no prompt text, no user text, no memory text and no tool
arguments other than profile schema keys ever reach ordinary reports. The tail
specialized runner can explicitly enable credential-scrubbed memory-query audit.
"""

from __future__ import annotations

import contextvars
import json

from tools.legacy import upgrade
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT / "tools" / "output"
OWNER_MARKER = ".violet-testchat-owner"

# Only these keys are taken from env_example / the tester's config file.
ALLOWED_KEYS = re.compile(
    r"^((INTENT|CHAT|MEMORY|PROACTIVE|PERFORMER)_LLM_(MODEL|PROMPT|MODEL_TEMPERATURE|FALLBACK_MODEL)"
    r"|LLM_REQUEST_TIMEOUT_SECONDS|LLM_CONNECT_TIMEOUT_SECONDS|LLM_MAX_RETRY_ATTEMPTS|LLM_TOTAL_TIMEOUT_SECONDS"
    r"|VIOLET_TIMEZONE|VIOLET_TEST_API_URL|VIOLET_PERSONA_REPLY_GUARD|VIOLET_SEMANTIC_GUARD|VIOLET_SEMANTIC_GUARD_MODEL"
    r"|VIOLET_INTENT_HIDE_EMPTY_MEMORY_TOOL|VIOLET_MEMORY_FINISH_SIGNAL|VIOLET_CONTEXT_HIDE_LEGACY_NICKNAME"
    r"|VIOLET_REMINDER_BEFORE_REPLY"
    # phase 9.1 memory switches (a test config may turn them off to compare with the 9.0 behaviour)
    r"|VIOLET_MEMORY_WORD_RECALL|VIOLET_MEMORY_WORD_RECALL_LIMIT|VIOLET_MEMORY_NOT_FOUND_NOTE|VIOLET_MEMORY_SEARCH_WINDOW"
    r"|VIOLET_MEMORY_KEEP_CLEAR_WEIGHT)$")
# Whatever the config says, these are forced (isolation + "no extras" defaults of the test tool).
FORCED = {
    "MCP_SERVERS": "", "VIOLET_WEB_ENABLED": "0", "VIOLET_WEB_EXPLORER": "0", "VIOLET_WEB_SEARCH_PROVIDER": "none",
    "VIOLET_IMAGE_ENABLED": "0", "VIOLET_LIFE_ENGINE": "0", "DEBUG_MODE": "0",
    "VIOLET_LOG_FILE": "", "VIOLET_STARTUP_MESSAGE": "",
    "DEBOUNCE_NORMAL": "0.05", "DEBOUNCE_EXTRA": "0.1", "VIOLET_SIMULATE_TYPING": "0", "VIOLET_SEND_RETRY_DELAY_SECONDS": "0",
    "VIOLET_MEMORY_RETRY_BASE_SECONDS": "0", "VIOLET_DASHBOARD_HOST": "127.0.0.1",
}
MOCK_KEY = "mock-key-not-a-real-key"

# Directories created by prepare_environment() in THIS process -> owner token.
# cleanup() deletes nothing else: no globbing, no directories from earlier runs.
_CREATED: dict[Path, str] = {}


class IsolationError(RuntimeError):
    pass


class ReplayError(RuntimeError):
    """A replayed round could not be reproduced exactly (phase 7 B.3)."""


class ScenarioClock:
    """Violet's clock in an evaluation session: starts at a given moment of the scenario and runs on with
    real elapsed time. Replays and continued runs use it so that the host's current date never
    replaces the scenario's own timeline (phase 7 B.3)."""

    def __init__(self, start: datetime):
        self.set(start)

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("the scenario clock needs a timezone-aware moment")
        self._base, self._t0 = when, time.monotonic()

    def now(self) -> datetime:
        # arithmetic in UTC: adding to a zone-aware datetime would add wall-clock time (wrong across DST)
        from ai.settings import local_timezone
        elapsed = timedelta(seconds=time.monotonic() - self._t0)
        return (self._base.astimezone(timezone.utc) + elapsed).astimezone(local_timezone())

    def advance(self, hours: float) -> datetime:
        """Move the scenario forward by real elapsed hours (phase 7 B.4). Stored history is never rewritten."""
        if hours < 0:
            raise ValueError("scenario time only moves forward")
        self.set(self.now().astimezone(timezone.utc) + timedelta(hours=hours))
        return self.now()

    def advance_to(self, when: datetime) -> datetime:
        if when.tzinfo is None:
            raise ValueError("the scenario clock needs a timezone-aware moment")
        if when < self.now():
            raise ValueError(f"scenario time only moves forward ({when.isoformat()} is in the past)")
        self.set(when)
        return self.now()


# modules that bind ai.settings.now_local by name at import time
_CLOCK_MODULES = ("ai.settings", "ai.memory_model", "ai.life_engine", "ai.life_scheduler")


def install_clock(clock: "ScenarioClock") -> list[tuple[Any, Any]]:
    import importlib
    saved = []
    for name in _CLOCK_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "now_local"):
            saved.append((module, module.now_local))
            module.now_local = clock.now
    return saved


def uninstall_clock(saved: list[tuple[Any, Any]]) -> None:
    for module, original in saved:
        module.now_local = original


class CleanupError(RuntimeError):
    """The test directory could not be removed completely (the message lists what is left)."""


def _parse_env_file(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def prepare_environment(workdir: Path | None = None, config_file: Path | None = None,
                        environ: dict[str, str] | None = None) -> Path:
    """Point every Violet path at a throw-away directory. Returns that directory."""
    environ = os.environ if environ is None else environ
    created = workdir is None or not Path(workdir).exists()
    workdir = Path(workdir or tempfile.mkdtemp(prefix="violet-testchat-")).resolve()
    production = {(PROJECT / "violet.db").resolve(), (PROJECT / "data").resolve(), (PROJECT / ".env").resolve()}
    if workdir in production or any(p in workdir.parents for p in production) or workdir == PROJECT:
        raise IsolationError(f"refusing to use {workdir}: not an isolated test directory")
    (workdir / "data").mkdir(parents=True, exist_ok=True)
    if created:
        token = uuid.uuid4().hex
        (workdir / OWNER_MARKER).write_text(token, encoding="utf-8")
        _CREATED[workdir] = token
    else:
        _CREATED.pop(workdir, None)               # reused by this run: owned by an earlier one, never deleted here
    empty_env = workdir / "empty.env"
    empty_env.write_text("# intentionally empty: the test tool never reads a real .env\n", encoding="utf-8")
    values: dict[str, str] = {}
    example = PROJECT / "env_example"
    if example.exists():
        values.update({k: v for k, v in _parse_env_file(example).items() if ALLOWED_KEYS.match(k)})
    if config_file:
        cfg = _parse_env_file(Path(config_file))
        refused = sorted(k for k in cfg if not ALLOWED_KEYS.match(k) and cfg[k] != "")
        if any("KEY" in k or "TOKEN" in k or "SECRET" in k for k in refused):
            raise IsolationError("the test config file must not contain keys or tokens; use VIOLET_TEST_API_KEY "
                                 "or the interactive prompt")
        values.update({k: v for k, v in cfg.items() if ALLOWED_KEYS.match(k)})
        values["_ignored_config_keys"] = ",".join(refused)
    for key in [k for k in environ if k.startswith(("VIOLET_", "LLM_", "MCP_")) or k.endswith("_LLM_MODEL")]:
        if key not in ("VIOLET_TEST_ALLOW_REAL_API", "VIOLET_TEST_API_KEY", "VIOLET_TEST_API_URL", "VIOLET_TEST_SEARCH_API_KEY"):
            environ.pop(key, None)                        # nothing from the tester's shell leaks into Violet
    environ.update({k: v for k, v in values.items() if not k.startswith("_")})
    environ.update(FORCED)
    environ.update({
        "VIOLET_ENV_FILE": str(empty_env), "VIOLET_DB_PATH": str(workdir / "violet-test.db"),
        "VIOLET_DATA_DIR": str(workdir / "data"), "VIOLET_MEDIA_DIR": str(workdir / "media"),
        "VIOLET_ADMIN_TOKEN": uuid.uuid4().hex, "LLM_API_KEY": MOCK_KEY,
        "LLM_API_URL": "http://127.0.0.1:9/v1",                 # replaced only by RealApiGate
    })
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        environ.pop(proxy, None)
    return workdir


SEED_FILES = ("violet-test.db", ".violet-eval-state.json")


def _file_sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seed_workdir(source: Path, target: Path) -> dict[str, Any]:
    """Copy a kept test directory's database and evaluation state into a NEW test directory (phase 7 B.6.2).
    The source is only read; it is checked to be unchanged afterwards. A non-empty write-ahead log next to
    the database is refused (committed data may still be in it, the copy would miss it); an EMPTY one only
    means something opened the file without writing, and is recorded."""
    source, target = Path(source).resolve(), Path(target).resolve()
    from tools.replay import read_state
    try:
        read_state(source)  # refuses synthetic markers/states before any database is copied
    except ValueError as exc:
        raise IsolationError(str(exc)) from exc
    if source == target:
        raise IsolationError("the seed directory and the new test directory are the same")
    missing = [n for n in SEED_FILES if not (source / n).is_file()]
    if missing:
        raise IsolationError(f"{source} 不是可用的测试目录（缺少 {missing}）")
    wal = source / "violet-test.db-wal"
    if wal.exists() and wal.stat().st_size > 0:
        raise IsolationError(f"{source} 的数据库还有未合并的写入日志（violet-test.db-wal {wal.stat().st_size} 字节），"
                             f"复制可能不完整，拒绝使用")
    leftovers = [n for n in ("violet-test.db-wal", "violet-test.db-shm") if (source / n).exists()]
    present = [n for n in SEED_FILES if (target / n).exists()]
    if present:
        raise IsolationError(f"{target} 里已经有 {present}，拒绝覆盖")
    before = {n: _file_sha256(source / n) for n in SEED_FILES}
    for name in SEED_FILES:
        shutil.copy2(source / name, target / name)
    after = {n: _file_sha256(source / n) for n in SEED_FILES}
    copied = {n: _file_sha256(target / n) for n in SEED_FILES}
    if before != after or before != copied:
        raise IsolationError(f"复制 {source} 时文件发生了变化，拒绝使用这份副本")
    return {"source": str(source), "sha256": before,
            "empty_wal_files_not_copied": leftovers}


def created_this_run(workdir: Path) -> bool:
    workdir = Path(workdir).resolve()
    token = _CREATED.get(workdir)
    marker = workdir / OWNER_MARKER
    try:
        return token is not None and marker.read_text(encoding="utf-8") == token
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Real API gate
# ---------------------------------------------------------------------------

@dataclass
class Budget:
    max_calls_per_turn: int = 15
    max_calls_total: int = 120
    max_tokens_total: int = 300_000
    price_per_1k_tokens: float | None = None          # None = unknown -> only the call / token caps apply
    max_cost: float | None = None
    # Phase 7 B.10.1: before a call, keep room for it. Its size is unknown until the provider answers, so the
    # reserve is an estimate: the largest call this agent made so far (else the largest call of any agent,
    # else first_call_reserve) times reserve_factor. Estimates are kept apart from the usage the provider reported.
    first_call_reserve: int = 16_000
    reserve_factor: float = 1.1

    def describe(self) -> str:
        price = (f"{self.price_per_1k_tokens} / 1K tokens, cost cap {self.max_cost}" if self.price_per_1k_tokens
                 else "price unknown -> conservative call and token caps only")
        return (f"max {self.max_calls_per_turn} model calls per round, {self.max_calls_total} in total, "
                f"{self.max_tokens_total} tokens in total (a call is refused when the tokens used so far plus a "
                f"reserve for it would pass the cap); {price}")


TOKEN_CAP_LIMITATION = ("the token cap is checked before each call against the usage reported so far plus an "
                        "estimated reserve for that call; the provider has no per-request total-token limit here, "
                        "so one call larger than its reserve can still end above the cap")


class BudgetExceeded(RuntimeError):
    """Not an OpenAI error: Violet's agent fails this call and replies with its fallback text."""


def mask(secret: str) -> str:
    return f"{secret[:3]}…{secret[-2:]} ({len(secret)} chars)" if secret and len(secret) > 8 else "***"


class RealApiGate:
    """Real model calls need ALL of: VIOLET_TEST_ALLOW_REAL_API=1, --real, a test key, and confirmation."""

    def __init__(self, environ: dict[str, str] | None = None, input_fn: Callable[[str], str] = input,
                 key_prompt: Callable[[str], str] | None = None):
        self.environ = os.environ if environ is None else environ
        self.input_fn = input_fn
        self.key_prompt = key_prompt

    def open(self, real_requested: bool, api_url: str | None, budget: Budget, models: dict[str, str],
             assume_yes: bool = False, out: Callable[[str], None] = print) -> str | None:
        """Return the key if every condition is met, else None (mock mode)."""
        if not real_requested:
            return None
        if self.environ.get("VIOLET_TEST_ALLOW_REAL_API") != "1":
            raise PermissionError("real API refused: set VIOLET_TEST_ALLOW_REAL_API=1 for this shell first")
        url = (api_url or self.environ.get("VIOLET_TEST_API_URL") or "").strip()
        if not url.lower().startswith(("https://", "http://")):
            raise PermissionError("real API refused: give the test endpoint with --api-url or VIOLET_TEST_API_URL")
        key = self.environ.get("VIOLET_TEST_API_KEY", "")
        if not key and self.key_prompt is not None:
            key = self.key_prompt("测试 API Key（不会显示，也不会保存）: ")
        if not key or key == MOCK_KEY:
            raise PermissionError("real API refused: no test key (VIOLET_TEST_API_KEY or the prompt)")
        out("=" * 72)
        out("即将调用真实模型（只用虚构数据；不连微信；不用生产数据库）")
        out(f"  接口: {url}")
        out(f"  Key : {mask(key)}")
        for role, model in models.items():
            out(f"  {role:<10} 模型: {model}")
        out(f"  预算: {budget.describe()}")
        out("  随时 Ctrl+C 停止；达到任一上限会立即停止调用。")
        out("=" * 72)
        if not assume_yes and self.input_fn("确认开始真实调用？输入 yes 继续: ").strip().lower() != "yes":
            raise PermissionError("real API not confirmed")
        self.environ["LLM_API_KEY"] = key
        self.environ["LLM_API_URL"] = url
        return key


# ---------------------------------------------------------------------------
# Usage / request statistics (allow-listed, content free)
# ---------------------------------------------------------------------------

def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def usage_fields(usage: Any) -> dict[str, Any]:
    """Token fields exactly as the provider reported them; a missing field stays None (never inferred)."""
    return {
        "usage_present": usage is not None,
        "prompt_tokens": _int_or_none(_field(usage, "prompt_tokens")),
        "completion_tokens": _int_or_none(_field(usage, "completion_tokens")),
        "total_tokens": _int_or_none(_field(usage, "total_tokens")),
        "cached_tokens": _int_or_none(_field(_field(usage, "prompt_tokens_details"), "cached_tokens")),
        "reasoning_tokens": _int_or_none(_field(_field(usage, "completion_tokens_details"), "reasoning_tokens")),
    }


def billed_tokens(fields: dict[str, Any]) -> int:
    """What the budget counts: the reported total, else prompt + completion, else 0 (usage missing)."""
    if fields.get("total_tokens") is not None:
        return int(fields["total_tokens"])
    return int(fields.get("prompt_tokens") or 0) + int(fields.get("completion_tokens") or 0)


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):          # multimodal parts: count the text parts only
        return sum(len(p.get("text") or "") for p in content if isinstance(p, dict))
    return 0


def request_stats(kwargs: dict[str, Any]) -> dict[str, int]:
    messages = kwargs.get("messages") or []
    chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        chars += _content_chars(m.get("content"))
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"], ensure_ascii=False, default=str))
    tools = kwargs.get("tools") or []
    return {"messages": len(messages), "request_chars": chars, "tools": len(tools),
            "tools_chars": len(json.dumps(tools, ensure_ascii=False)) if tools else 0}


_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,40}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]{0,40}$")
_ID_FIELDS = ("memory_id", "fact_id", "commitment_id", "situation_id", "preference_id")
_LIST_FIELDS = ("memories", "candidates", "fact_ids", "results")


def redact_for_log(value: Any, scrub: Callable[[str], str] = lambda s: s) -> Any:
    """A copy of tool arguments / results for a local test log (phase 9.1.1): secrets scrubbed, and every string the
    profile rules treat as sensitive (phone, ID card, e-mail, bank card, exact address, health / finance words)
    replaced by its length only."""
    from ai.profile_schema import is_sensitive
    if isinstance(value, str):
        text = scrub(value)
        return f"[敏感内容已隐藏，{len(text)} 字]" if is_sensitive("other", text) else text
    if isinstance(value, dict):
        return {k: redact_for_log(v, scrub) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_for_log(v, scrub) for v in value]
    return value


def summarize_tool_result(name: str, arguments: dict[str, Any], result_text: str) -> dict[str, Any]:
    """Codes, booleans, counts and ids only -- free text (values, errors, notes) never leaves the process."""
    info: dict[str, Any] = {"tool": name}
    key = (arguments or {}).get("key")
    if isinstance(key, str) and _KEY_RE.match(key):
        info["key"] = key                                   # profile schema key such as "study.major"
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        info["result"] = "non_json"
        return info
    if not isinstance(payload, dict):
        info["result"] = type(payload).__name__
        return info
    if isinstance(payload.get("ok"), bool):
        info["ok"] = payload["ok"]
    for k in ("action", "reason", "error_code", "status"):
        v = payload.get(k)
        if isinstance(v, str) and _CODE_RE.match(v):
            info[k] = v
    if "error" in payload or "message" in payload and payload.get("ok") is False:
        info["has_error_text"] = True
    for k in _ID_FIELDS:
        v = _int_or_none(payload.get(k))
        if v is not None:
            info[k] = v
    for k in _LIST_FIELDS:
        v = payload.get(k)
        if isinstance(v, list):
            info[f"{k}_count"] = len(v)
            ids = [_int_or_none(x.get("id")) if isinstance(x, dict) else _int_or_none(x) for x in v]
            ids = [i for i in ids if i is not None]
            if ids:
                info[f"{k}_ids"] = ids[:20]
    return info


# ---------------------------------------------------------------------------
# Call recording + budget enforcement (wraps whatever client the agents use)
# ---------------------------------------------------------------------------

_AGENT: contextvars.ContextVar[str] = contextvars.ContextVar("violet_test_agent", default="?")
_RUN: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("violet_test_run", default=None)
_LAST_CALL: contextvars.ContextVar[Any] = contextvars.ContextVar("violet_test_last_call", default=None)


@dataclass
class CallRecord:
    agent: str
    model: str
    started_at: str
    latency_ms: int
    ok: bool
    error: str | None = None                     # scrubbed free text: console only, never in call details
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_calls: list[str] = field(default_factory=list)
    round: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_present: bool = False
    call_no: int = 0
    round_call_no: int = 0
    run_id: int | None = None
    step: int | None = None
    messages: int = 0
    request_chars: int = 0
    tools: int = 0
    tools_chars: int = 0
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    error_type: str | None = None
    status_code: int | None = None

    @property
    def billed(self) -> int:
        return billed_tokens(self.__dict__)


DETAIL_FIELDS = ("call_no", "round", "round_call_no", "agent", "model", "run_id", "step", "messages",
                 "request_chars", "tools", "tools_chars", "tool_calls", "tool_results", "latency_ms", "ok",
                 "error_type", "status_code", "usage_present", "prompt_tokens", "completion_tokens", "total_tokens",
                 "cached_tokens", "reasoning_tokens")


def call_detail(rec: CallRecord) -> dict[str, Any]:
    """The allow-listed, content-free view of one model call (what reports contain)."""
    return {k: getattr(rec, k) for k in DETAIL_FIELDS}


def error_label(rec: CallRecord | dict[str, Any]) -> str:
    get = rec.get if isinstance(rec, dict) else (lambda k: getattr(rec, k))
    label = get("error_type") or "error"
    return f"{label} (HTTP {get('status_code')})" if get("status_code") else label


def execution_errors(jobs: list[dict[str, Any]], refused: list[dict[str, Any]], turns: list[dict[str, Any]],
                     scrub: Callable[[str], str] = lambda s: s) -> list[dict[str, Any]]:
    """What did not run to the end in a round, apart from API errors (phase 7 B.10.1): unfinished memory jobs,
    calls the budget refused (never sent), user turns that failed. A round with none of them still needs its checks."""
    errors = [{"kind": "memory_job", "job_id": j["job_id"], "status": j["status"], "attempts": j["attempts"],
               "error_type": j["error_type"], "error": j["error"]} for j in jobs if j["status"] != "done"]
    errors += [{"kind": "budget_refused", "agent": r["agent"], "reason": r["reason"]} for r in refused]
    errors += [{"kind": "turn", "inbound_id": t["id"], "status": t["status"], "error": scrub(t["error"] or "")[:300] or None}
               for t in turns if t["status"] != "done"]
    return errors


class CallRecorder:
    def __init__(self, budget: Budget, secrets: list[str] | None = None):
        self.budget = budget
        self.calls: list[CallRecord] = []
        self.runs: list[dict[str, Any]] = []
        self.round: int | None = None
        self.turn_calls = 0
        self.stopped: str | None = None
        self.secrets = [s for s in (secrets or []) if s]
        self.last_request: dict[str, dict[str, Any]] = {}
        self.replay_calls = 0                    # offline answers served during a replay: never billed
        self.refused: list[dict[str, Any]] = []  # calls refused by the budget: never sent, never billed
        self.stop_detail: dict[str, Any] | None = None
        # phase 9.1.1 (free_chat): called with each CallRecord right after it is recorded (a per-call usage ledger that
        # survives a killed process); the largest earlier call of each agent in previous launches, for the reserve of
        # the first call of a new launch; and, when a list, the redacted MemoryAgent tool arguments and results.
        self.on_record: Callable[[CallRecord], None] | None = None
        self.prior_sizes: dict[str, int] = {}
        self.tool_log: list[dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def start_round(self, number: int | None) -> None:
        with self._lock:
            self.round, self.turn_calls = number, 0

    def start_run(self, agent: str) -> dict[str, Any]:
        with self._lock:
            run = {"run_id": len(self.runs) + 1, "round": self.round, "agent": agent, "steps": 0, "ok": None,
                   "finished_after_tools": False, "error_type": None}
            self.runs.append(run)
            return run

    @property
    def tokens(self) -> int:
        return sum(c.billed for c in self.calls)

    @property
    def cost(self) -> float | None:
        if not self.budget.price_per_1k_tokens:
            return None
        return round(self.tokens / 1000 * self.budget.price_per_1k_tokens, 6)

    def reserve_for(self, agent: str | None) -> tuple[int, str]:
        """Estimated tokens of the next call of `agent` -- an estimate, never added to the reported usage."""
        b = self.budget
        own = [c.billed for c in self.calls if c.agent == agent and c.ok]
        if not own and agent in self.prior_sizes:
            own = [self.prior_sizes[agent]]                  # this agent's largest call in an earlier launch
        if own:
            return int(max(own) * b.reserve_factor + 0.999), f"largest earlier {agent} call x {b.reserve_factor}"
        seen = [c.billed for c in self.calls if c.ok] + list(self.prior_sizes.values())
        if seen:
            return (int(max(seen) * b.reserve_factor + 0.999),
                    f"no earlier {agent} call: largest earlier call x {b.reserve_factor}")
        return b.first_call_reserve, "no earlier call: first_call_reserve"

    def _refuse(self, agent: str | None, reason: str, stop: bool, detail: dict[str, Any] | None = None) -> None:
        if stop:
            self.stopped = reason
            self.stop_detail = {"reason": reason, "round": self.round, "agent": agent, **(detail or {})}
        self.refused.append({"round": self.round, "agent": agent, "reason": reason})
        raise BudgetExceeded(reason)

    def check(self, agent: str | None = None) -> None:
        with self._lock:
            b = self.budget
            if self.stopped:
                self._refuse(agent, self.stopped, stop=False)
            # global caps first: once one is hit the whole session stops for good
            used = self.tokens
            if len(self.calls) >= b.max_calls_total:
                self._refuse(agent, f"total call cap {b.max_calls_total} reached", stop=True,
                             detail={"used_tokens": used, "remaining_tokens": b.max_tokens_total - used})
            reserve, basis = self.reserve_for(agent)
            if used + reserve > b.max_tokens_total:
                reason = (f"token cap {b.max_tokens_total}: {used} used + {reserve} reserved for the next "
                          f"{agent} call would pass it" if used < b.max_tokens_total else
                          f"token cap {b.max_tokens_total} reached")
                self._refuse(agent, reason, stop=True,
                             detail={"used_tokens": used, "remaining_tokens": b.max_tokens_total - used,
                                     "reserved_tokens": reserve, "reserve_basis": basis})
            if b.max_cost is not None and self.cost is not None and self.cost >= b.max_cost:
                self._refuse(agent, f"cost cap {b.max_cost} reached", stop=True, detail={"used_tokens": used})
            if self.turn_calls >= b.max_calls_per_turn:
                self._refuse(agent, f"per-round call cap {b.max_calls_per_turn} reached", stop=False)
            self.turn_calls += 1

    def budget_state(self) -> dict[str, Any]:
        """Reported usage, the caps and why the session stopped. Reserves are estimates, listed apart."""
        b = self.budget
        return {"max_tokens_total": b.max_tokens_total, "used_tokens": self.tokens,
                "remaining_tokens": b.max_tokens_total - self.tokens, "max_calls_total": b.max_calls_total,
                "calls": len(self.calls), "max_calls_per_turn": b.max_calls_per_turn,
                "reserve_policy": {"first_call_reserve": b.first_call_reserve, "reserve_factor": b.reserve_factor},
                "stopped": self.stopped, "stop_detail": self.stop_detail, "refused_calls": list(self.refused),
                "limitation": TOKEN_CAP_LIMITATION}

    def scrub(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def record(self, rec: CallRecord) -> None:
        if rec.error:
            rec.error = self.scrub(rec.error)
        with self._lock:
            rec.round = self.round
            rec.call_no = len(self.calls) + 1
            rec.round_call_no = sum(1 for c in self.calls if c.round == self.round) + 1
            self.calls.append(rec)
        if self.on_record is not None:
            try:
                self.on_record(rec)
            except Exception:                    # bookkeeping must never fail the call it records
                logging.getLogger("violet.harness").exception("on_record hook failed")


class BudgetedClient:
    """Duck-types `client.chat.completions.create`; enforces the budget BEFORE every call.

    phase_fn() == "replay" routes the call to replay_fn: no budget check, no record, and the real
    inner client is not even created (inner_factory runs on the first live call only)."""

    def __init__(self, inner, recorder: CallRecorder, *, inner_factory: Callable[[], Any] | None = None,
                 phase_fn: Callable[[], str] | None = None, replay_fn: Callable[..., Any] | None = None):
        self._inner = inner
        self._inner_factory = inner_factory
        self._recorder = recorder
        self._phase_fn = phase_fn
        self._replay_fn = replay_fn
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {})()
        self.chat.completions.create = self._create

    def _create(self, **kwargs):
        if self._phase_fn is not None and self._phase_fn() == "replay":
            with self._recorder._lock:
                self._recorder.replay_calls += 1
            return self._replay_fn(**kwargs)
        agent = _AGENT.get()
        self._recorder.check(agent)
        self._recorder.last_request[agent] = kwargs
        run = _RUN.get()
        if run is not None:
            run["steps"] += 1
        stats = request_stats(kwargs)
        started, at = time.monotonic(), datetime.now().isoformat(timespec="seconds")
        base = dict(agent=agent, model=str(kwargs.get("model")), started_at=at,
                    run_id=run["run_id"] if run else None, step=run["steps"] if run else None, **stats)
        try:
            if self._inner is None:
                self._inner = self._inner_factory()
            response = self._inner.chat.completions.create(**kwargs)
        except Exception as exc:
            # Test keys must not reach pipeline logs, SQLite error rows, or model error messages.
            # Preserve SDK exception types/status so Violet's existing retry classification still applies.
            if self._recorder.secrets:
                exc.args = tuple(self._recorder.scrub(a) if isinstance(a, str) else a for a in exc.args)
                if isinstance(getattr(exc, "message", None), str):
                    exc.message = self._recorder.scrub(exc.message)
                if getattr(exc, "body", None) is not None:
                    exc.body = json.loads(self._recorder.scrub(json.dumps(exc.body, ensure_ascii=False, default=str)))
            status = getattr(exc, "status_code", None)
            self._recorder.record(CallRecord(**base, latency_ms=int((time.monotonic() - started) * 1000), ok=False,
                                             error=f"{exc.__class__.__name__}: {str(exc)[:200]}",
                                             error_type=exc.__class__.__name__,
                                             status_code=status if isinstance(status, int) else None))
            raise
        if self._recorder.secrets and hasattr(response, "model_dump"):
            # A provider can echo a credential in content/tool arguments. Scrub before Violet stores it.
            def safe(value):
                if isinstance(value, str):
                    return self._recorder.scrub(value)
                if isinstance(value, list):
                    return [safe(v) for v in value]
                if isinstance(value, dict):
                    return {self._recorder.scrub(k) if isinstance(k, str) else k: safe(v) for k, v in value.items()}
                return value
            response = type(response).model_validate(safe(response.model_dump()))
        message = response.choices[0].message if getattr(response, "choices", None) else None
        tools = [tc.function.name for tc in (getattr(message, "tool_calls", None) or [])]
        rec = CallRecord(**base, latency_ms=int((time.monotonic() - started) * 1000), ok=True, tool_calls=tools,
                         **usage_fields(getattr(response, "usage", None)))
        self._recorder.record(rec)
        _LAST_CALL.set(rec)                    # tool results executed next in this run attach to this call
        return response


def summarize_calls(calls: list[CallRecord | dict[str, Any]], runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Statistics only. A fitted intercept is a statistic, not a confirmed fixed overhead."""
    rows = [call_detail(c) if isinstance(c, CallRecord) else c for c in calls]
    runs = runs or []

    def total(key: str, subset: list[dict[str, Any]]) -> dict[str, Any]:
        values = [r[key] for r in subset if r.get(key) is not None]
        return {"sum": sum(values) if values else None, "calls_reporting": len(values)}

    by_agent: dict[str, Any] = {}
    for agent in sorted({r["agent"] for r in rows}):
        sub = [r for r in rows if r["agent"] == agent]
        lat = [r["latency_ms"] for r in sub]
        tool_counts: dict[str, int] = {}
        for r in sub:
            for t in r["tool_calls"]:
                tool_counts[t] = tool_counts.get(t, 0) + 1
        agent_runs = [r for r in runs if r["agent"] == agent]
        by_agent[agent] = {
            "calls": len(sub), "errors": sum(1 for r in sub if not r["ok"]),
            "latency_ms_avg": int(sum(lat) / len(lat)) if lat else 0, "latency_ms_max": max(lat) if lat else 0,
            **{k: total(k, sub) for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                           "cached_tokens", "reasoning_tokens")},
            "request_chars_avg": int(sum(r["request_chars"] for r in sub) / len(sub)) if sub else 0,
            "tools_chars_avg": int(sum(r["tools_chars"] for r in sub) / len(sub)) if sub else 0,
            "tool_calls": tool_counts,
            "runs": len(agent_runs),
            "runs_finished_after_tools": sum(1 for r in agent_runs if r.get("finished_after_tools")),
        }
    diffs = [r["total_tokens"] - r["prompt_tokens"] - r["completion_tokens"] for r in rows
             if None not in (r.get("total_tokens"), r.get("prompt_tokens"), r.get("completion_tokens"))]
    return {
        "calls": len(rows), "errors": sum(1 for r in rows if not r["ok"]),
        "usage_missing_calls": sum(1 for r in rows if r["ok"] and not r["usage_present"]),
        "tokens": {k: total(k, rows) for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                                "cached_tokens", "reasoning_tokens")},
        "total_vs_prompt_plus_completion": {"calls_compared": len(diffs),
                                            "calls_differing": sum(1 for d in diffs if d),
                                            "max_abs_difference": max((abs(d) for d in diffs), default=0)},
        "by_agent": by_agent,
        "fit_prompt_tokens_vs_request_chars": fit_prompt_tokens(rows),
    }


def fit_prompt_tokens(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Least squares prompt_tokens ~ a + b * (request_chars + tools_chars). Descriptive statistics only."""
    points = [(r["request_chars"] + r["tools_chars"], r["prompt_tokens"]) for r in rows
              if r.get("prompt_tokens") is not None]
    note = ("descriptive statistic over the calls of this run; the intercept is NOT a confirmed fixed overhead "
            "(tokenizer, caching and provider behaviour are unknown)")
    if len(points) < 3:
        return {"n": len(points), "note": "fewer than 3 calls with prompt_tokens: no fit"}
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return {"n": n, "note": "all requests have the same size: no fit"}
    sxy = sum((x - mx) * (y - my) for x, y in points)
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for _, y in points)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in points)
    return {"n": n, "slope_tokens_per_char": round(slope, 4), "intercept_tokens": round(intercept, 1),
            "r2": round(1 - ss_res / ss_tot, 4) if ss_tot else None, "note": note}


# ---------------------------------------------------------------------------
# Mock model (default)
# ---------------------------------------------------------------------------

class MockLLM:
    """Offline stand-in: valid JSON for intent / memory / proactive, a short echo for chat."""

    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {})()
        self.chat.completions.create = self._create

    @staticmethod
    def _completion(content: str, model: str):
        from openai.types.chat import ChatCompletion
        return ChatCompletion.model_validate({
            "id": f"mock-{uuid.uuid4().hex[:8]}", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def _create(self, **kwargs):
        agent = _AGENT.get()
        model = str(kwargs.get("model"))
        if agent == "IntentAgent":
            content = json.dumps({"route": "chat", "should_reply": True, "user_intent": "mock", "emotion": "neutral",
                                  "silent_reason": None, "active_task_id": None,
                                  "task": {"exists": False, "name": None, "description": None}})
        elif agent == "MemoryAgent":
            content = json.dumps({"should_save": False, "reason": "mock"})
        elif agent == "ProactiveAgent":
            content = json.dumps({"should_act": False, "action_type": "none", "reason": "mock: nothing worth sending"})
        elif agent.startswith("AuxLLM:"):
            content = json.dumps({"verdict": "ok", "violations": [], "proposals": []})
        else:
            user = next((m.get("content") for m in reversed(kwargs.get("messages") or [])
                         if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
            content = f"（模拟回复）收到：{user[:30]}"
        return self._completion(content, model)


# ---------------------------------------------------------------------------
# Offline mock images (phase 7 B.8.1)
# ---------------------------------------------------------------------------

MOCK_IMAGE_ENTRY = "mock/mock-image-1"
IMAGE_PROVIDER_FILE = "image_providers.mock.json"


def write_mock_image_config(workdir: Path, mock_options: dict[str, Any] | None = None) -> Path:
    """The only image configuration a --image-mock run uses: one offline mock model, nothing else."""
    data = {
        "_comment": ["Written by the test tool for --image-mock: offline mock adapter only, no network, no key."],
        "version": 1,
        "budgets": {"global": {"max_images_per_day": 20, "max_cost_per_day": 1.0, "currency": "USD"}},
        "providers": [{
            "provider_id": "mock", "adapter_type": "mock", "enabled": True, "timeout": 5,
            "models": [{
                "model_id": "mock-image-1", "enabled": True,
                "status_note": "test tool: offline mock pictures (flat-colour PNG), never a real provider",
                "capabilities": {"vision": "verified", "image_generation": "verified", "image_edit": "verified",
                                 "return_base64": "verified"},
                "limits": {"max_outputs": 1, "max_input_images": 2, "max_concurrency": 1},
                "pricing": {"free": True}, "priority": 1,
                "options": {"mock": dict(mock_options or {})},
            }],
        }],
    }
    path = Path(workdir) / IMAGE_PROVIDER_FILE
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def validate_image_config(path: Path) -> list[str]:
    """Refuse anything but offline mock models: other adapter types, endpoints, key variables, URL outputs
    (a URL would be downloaded). Checked on the raw file AND through the product's own registry."""
    try:
        data = upgrade(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise IsolationError(f"图片配置 {path} 无法读取：{exc.__class__.__name__}") from None
    problems = []
    for provider in data.get("providers") or []:
        for model in [{}] + list(provider.get("models") or []):
            merged = {**{k: v for k, v in provider.items() if k != "models"}, **model}
            name = f"{provider.get('provider_id')}/{model.get('model_id', '*')}"
            if merged.get("adapter_type") != "mock":
                problems.append(f"{name}: adapter_type {merged.get('adapter_type')!r}")
            if merged.get("base_url") or merged.get("api_key_env"):
                problems.append(f"{name}: has an endpoint or a key variable")
            if ((merged.get("options") or {}).get("mock") or {}).get("return") == "url":
                problems.append(f"{name}: URL outputs would be downloaded")
    from ai.images.registry import ImageProviderRegistry
    registry = ImageProviderRegistry(db=None, path=Path(path))
    for entry in registry.entries.values():
        if entry.adapter_type != "mock":
            problems.append(f"{entry.entry_id}: adapter_type {entry.adapter_type!r}")
    usable = [e.entry_id for e in registry.entries.values()
              if e.enabled and not e.config_errors and e.state("image_generation") == "verified"]
    if not usable:
        problems.append("no enabled mock model that can generate pictures")
    if problems:
        raise IsolationError("--image-mock 只允许离线 Mock 图片模型，配置被拒绝：" + "；".join(problems))
    return usable


class RefusedImageAdapter:
    """Stands in for every non-mock image adapter during a --image-mock run: it never reaches a network."""
    attempts: list[str] = []

    def __init__(self, entry):
        self.entry = entry

    def run(self, request):
        from ai.images.adapters import AdapterError
        RefusedImageAdapter.attempts.append(self.entry.entry_id)
        raise AdapterError("blocked_by_test_tool", "only the offline mock image adapter may run in the test tool", "no")


def lock_image_adapters() -> Callable[[], None]:
    """Only the mock adapter stays; HTTP transport of image adapters refuses every request. Returns undo."""
    import httpx
    from ai.images import adapters
    saved_adapters, saved_factory = dict(adapters.ADAPTERS), adapters._TRANSPORT_FACTORY

    def refuse(request):
        raise httpx.ConnectError("test tool: image HTTP is blocked", request=request)
    for name in list(adapters.ADAPTERS):
        if name != "mock":
            adapters.ADAPTERS[name] = RefusedImageAdapter
    adapters.set_transport_factory(lambda entry: httpx.MockTransport(refuse))

    def restore() -> None:
        adapters.ADAPTERS.clear()
        adapters.ADAPTERS.update(saved_adapters)
        adapters._TRANSPORT_FACTORY = saved_factory
    return restore


class NetworkGuard:
    """Process-wide socket guard of a --image-mock run: only the listed endpoints (the real text-model API,
    if any) may be connected to. asyncio's local self-pipes (socket.socketpair) are let through and counted.
    Everything else -- image providers, WeChat, any other host, other local services -- is refused."""

    def __init__(self, allowed_urls: list[str] | None = None):
        import socket as _socket
        from urllib.parse import urlsplit
        self.allowed: set[tuple[str, int]] = set()
        self.endpoints: list[str] = []
        for url in allowed_urls or []:
            parts = urlsplit(url)
            if not parts.hostname:
                continue
            port = parts.port or (443 if parts.scheme == "https" else 80)
            self.endpoints.append(f"{parts.hostname}:{port}")
            try:
                import ipaddress
                self.allowed.add((str(ipaddress.ip_address(parts.hostname)), port))     # an address: nothing to resolve
            except ValueError:
                for info in _socket.getaddrinfo(parts.hostname, port, proto=_socket.IPPROTO_TCP):
                    self.allowed.add((info[4][0], port))
        self.blocked: list[str] = []
        self.allowed_connections = 0
        self.web_connections = 0                  # phase 9.3: one-shot passes granted by free_chat's web dialer
        self.local_pipes = 0
        self._local = threading.local()
        self._saved = None
        self.active_during_run = False            # stays True after uninstall (the report is written after close)

    def _check(self, address) -> None:
        if getattr(self._local, "pipe", False):
            self.local_pipes += 1
            return
        if getattr(self._local, "web_pass", False):
            self._local.web_pass = False
            self.web_connections += 1
            return
        if isinstance(address, tuple) and len(address) >= 2 and (str(address[0]), int(address[1])) in self.allowed:
            self.allowed_connections += 1
            return
        shown = f"{address[0]}:{address[1]}" if isinstance(address, tuple) and len(address) >= 2 else str(address)
        self.blocked.append(shown)
        raise ConnectionRefusedError(f"test tool network guard: connection to {shown} is not allowed")

    def pass_next_connect(self) -> None:
        """Phase 9.3 (free_chat --web on): let THIS thread's next connect through, once. Only the web dialer calls it,
        after SafeFetcher checked the pinned address (public, or the search provider's own host) and the port."""
        self._local.web_pass = True

    def clear_pass(self) -> None:
        self._local.web_pass = False

    def install(self) -> "NetworkGuard":
        import socket as _socket
        if self._saved is not None:
            return self
        self.active_during_run = True
        connect, connect_ex, socketpair = _socket.socket.connect, _socket.socket.connect_ex, _socket.socketpair
        self._saved = (connect, connect_ex, socketpair)
        guard = self

        def guarded_connect(sock, address):
            guard._check(address)
            return connect(sock, address)

        def guarded_connect_ex(sock, address):
            guard._check(address)
            return connect_ex(sock, address)

        def guarded_socketpair(*args, **kwargs):
            guard._local.pipe = True
            try:
                return socketpair(*args, **kwargs)
            finally:
                guard._local.pipe = False
        _socket.socket.connect, _socket.socket.connect_ex, _socket.socketpair = (
            guarded_connect, guarded_connect_ex, guarded_socketpair)
        return self

    def uninstall(self) -> None:
        import socket as _socket
        if self._saved is not None:
            _socket.socket.connect, _socket.socket.connect_ex, _socket.socketpair = self._saved
            self._saved = None

    def summary(self) -> dict[str, Any]:
        return {"allowed_endpoints": list(self.endpoints), "allowed_connections": self.allowed_connections,
                "web_connections": self.web_connections,
                "blocked_connections": list(self.blocked), "local_pipes": self.local_pipes,
                "installed": self._saved is not None, "active_during_run": self.active_during_run}


# ---------------------------------------------------------------------------
# WeChat stand-in
# ---------------------------------------------------------------------------

class LocalWeChat:
    """In-memory replacement for the iLink client: nothing ever leaves the process.

    image_dir (--image-mock): send_image copies the picture there and records it -- the only place a
    "sent" picture goes. Without it, sending a picture is refused as before."""

    def __init__(self, image_dir: Path | None = None):
        self.sent: list[dict[str, Any]] = []
        self.state = type("State", (), {"logged_in": True})()
        self.image_dir = Path(image_dir) if image_dir else None
        self.images: list[dict[str, Any]] = []
        self.current_round: int | None = None
        self.fail_images = 0                                   # tests: refuse the next N picture sends

    def fetch_messages(self, timeout: int = 35, persist_buf: bool = True):
        return [], timeout

    def commit_sync_buf(self) -> None:
        pass

    def download_media(self, item):
        raise RuntimeError("media is not supported by the test tool")

    def send_typing(self, status: int = 1, **kwargs):
        return {"ret": 0}

    def send_text(self, text: str, context_token=None, to_user_id=None, client_id=None):
        self.sent.append({"text": text, "at": datetime.now().isoformat(timespec="seconds")})
        return {"ret": 0}

    def send_image(self, path=None, *args, **kwargs):
        if self.image_dir is None:
            raise RuntimeError("images are disabled in the test tool")
        import hashlib
        source = Path(str(path))
        record = {"round": self.current_round, "sender": "local test sender (no WeChat)",
                  "at": datetime.now().isoformat(timespec="seconds")}
        if self.fail_images > 0:
            self.fail_images -= 1
            self.images.append({**record, "recorded": False, "error": "simulated send failure"})
            raise RuntimeError("simulated picture send failure (test tool)")
        data = source.read_bytes()
        self.image_dir.mkdir(parents=True, exist_ok=True)
        target = self.image_dir / f"{len(self.images) + 1:03d}-round{self.current_round or 0}{source.suffix or '.png'}"
        target.write_bytes(data)
        record.update(recorded=True, file=target.name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.images.append(record)
        with open(self.image_dir / "sent_images.jsonl", "a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"ret": 0}


# ---------------------------------------------------------------------------
# Time travel
# ---------------------------------------------------------------------------

AGE_COLUMNS = {
    "messages": ["created_at"],
    "event_memories": ["created_at", "event_time", "valid_until", "progress_updated_at", "last_user_mention_at"],
    "proactive_events": ["created_at", "updated_at"],
    "topic_mentions": ["created_at"],
    "commitments": ["created_at", "due_at", "next_attempt_at"],
    "inbound_messages": ["received_at"],
    "user_situations": ["observed_at", "expires_at", "created_at"],
    "user_profile_facts": ["stated_at", "confirmed_at", "created_at"],
    "profile_questions": ["created_at"],
    "preference_observations": ["observed_at"],
    "life_events": ["occurred_at", "created_at"],
    "life_threads": ["started_at", "last_progress_at", "created_at"],
    "life_plans": ["planned_for", "created_at"],
    "memory_jobs": ["created_at"],
}


def advance_time(db, hours: float) -> None:
    """LEGACY (phase 7 B.1-B.3): pretend `hours` passed by moving every recorded timestamp into the past.

    The tools no longer use it (phase 7 B.4): it rewrote message, evidence and event times and moved stored
    future dates (an event on 10-21 became 10-20). Time now passes on the session's ScenarioClock.
    Phase 9.1.1: a date-only event time ("2026-03-11") stays a date -- the day its end-of-day deadline falls on."""
    from ai.memory_model import event_deadline, is_date_only
    for table, cols in AGE_COLUMNS.items():
        try:
            rows = db.fetch_all(f"SELECT id, {', '.join(cols)} FROM {table}")
        except Exception:
            continue
        for row in rows:
            updates = {}
            for col in cols:
                if row.get(col):
                    try:
                        if is_date_only(row[col]):
                            updates[col] = (event_deadline(row[col]) - timedelta(hours=hours)).date().isoformat()
                        else:
                            updates[col] = (datetime.fromisoformat(row[col]) - timedelta(hours=hours)).isoformat()
                    except ValueError:
                        continue
            if updates:
                db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in updates)} WHERE id = ?",
                           (*updates.values(), row["id"]))
    for key in ("last_engaged_user_turn_at", "next_proactive_at"):
        value = db.get_proactive_setting(key)
        if value:
            db.set_proactive_setting(key, (datetime.fromisoformat(value) - timedelta(hours=hours)).isoformat())


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

_CONTEXT_REPORT_KEYS = ("mode", "sections", "fact_ids", "situation_ids", "history_messages", "trimmed_items",
                        "approx_tokens")


def _context_report(report: Any) -> dict[str, Any] | None:
    """ContextEngine's report holds section sizes and ids only; copy the allow-listed keys."""
    if not isinstance(report, dict):
        return None
    out = {k: report.get(k) for k in _CONTEXT_REPORT_KEYS if k in report}
    if isinstance(out.get("sections"), dict):
        out["sections"] = {str(k): int(v) for k, v in out["sections"].items() if isinstance(v, int)}
    return out


class VioletTestSession:
    def __init__(self, workdir: Path, recorder: CallRecorder, real: bool, db=None,
                 quiet_hours: str = "config", no_memory: bool = False, verbose: bool = False,
                 owns_db: bool | None = None, clock: ScenarioClock | None = None, image_mock: bool = False,
                 memory_audit=None):
        self.image_mock = bool(image_mock)
        self.network_guard: NetworkGuard | None = None
        self._image_restore: Callable[[], None] | None = None
        if self.image_mock:
            if os.environ.get("VIOLET_IMAGE_ENABLED") != "1" or not os.environ.get("VIOLET_IMAGE_PROVIDERS_FILE"):
                raise IsolationError("--image-mock needs the mock image configuration from open_session")
            validate_image_config(Path(os.environ["VIOLET_IMAGE_PROVIDERS_FILE"]))
        if clock is None:                         # every session runs on a scenario clock (phase 7 B.4)
            from ai.settings import now_local
            clock = ScenarioClock(now_local())
        self.clock = clock
        self._saved_clock = install_clock(clock)
        self.phase = "live"                       # "replay": answers come from a recorded report, offline
        self.replay_reply: str | None = None
        from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
        from ai.agents.base_agent import BaseAgent
        from ai.proactive_scheduler import ProactiveScheduler
        from violet_runtime import VioletRuntime, MessageSender
        # The CLI uses the process-wide connection, which points into the test directory: the session
        # owns it and closes it so Windows can delete the files. A connection whose file lives anywhere
        # else (e.g. the test suite's own database) is never closed here.
        if db is None:
            from ai.chat import violetdb as db
            if owns_db is None:
                owns_db = Path(workdir).resolve() in Path(db.db_path).resolve().parents
        self.owns_db = bool(owns_db)
        self.db = db
        if memory_audit is not None:
            memory_audit.db = db
        self.workdir = workdir
        self.recorder = recorder
        self.real = real
        self.verbose = verbose
        self.closed = False
        self.pipeline_log: list[str] = []
        self.memory_script: list[dict[str, Any]] | None = None
        self.memory_script_results: list[dict[str, Any]] = []
        self.seed: dict[str, Any] | None = None            # set by open_session(seed_from=...)
        self._saved =(BaseAgent.client_factory, BaseAgent._create_completion, BaseAgent.run_async,
                       BaseAgent._call_local_tool)

        def factory(cfg):
            def real_client():
                from openai import OpenAI, Timeout
                from ai.settings import env_float
                return OpenAI(api_key=cfg.api_key, base_url=cfg.api_url, max_retries=0,
                              timeout=Timeout(env_float("LLM_REQUEST_TIMEOUT_SECONDS", 90.0),
                                              connect=env_float("LLM_CONNECT_TIMEOUT_SECONDS", 10.0)))
            phase = (lambda: self.phase)
            if real:                                   # created on the first live call, never during a replay
                return BudgetedClient(None, recorder, inner_factory=real_client, phase_fn=phase,
                                      replay_fn=self._replay_response)
            return BudgetedClient(MockLLM(), recorder, phase_fn=phase, replay_fn=self._replay_response)

        original = BaseAgent._create_completion
        original_run_async = BaseAgent.run_async
        original_tool = BaseAgent._call_local_tool

        def tagged(agent_self, *a, **k):
            if memory_audit is not None:
                memory_audit.observe_chat(agent_self.__class__.__name__, k)
            token = _AGENT.set(agent_self.__class__.__name__)
            try:
                return original(agent_self, *a, **k)
            finally:
                _AGENT.reset(token)

        async def tracked_run(agent_self, *a, **k):
            run = recorder.start_run(agent_self.__class__.__name__)
            token = _RUN.set(run)
            try:
                result = await original_run_async(agent_self, *a, **k)
                run["ok"] = True
                run["finished_after_tools"] = bool(isinstance(result, dict) and result.get("finished_after_tools"))
                return result
            except BaseException as exc:
                run["ok"], run["error_type"] = False, exc.__class__.__name__
                raise
            finally:
                context = _context_report(getattr(agent_self, "last_context_report", None))
                if context is not None and agent_self.__class__.__name__ == "ChatAgent":
                    run["context"] = context
                _RUN.reset(token)

        async def summarized_tool(agent_self, handler, tool_name, arguments):
            if memory_audit is not None and tool_name == "query_memory":
                with memory_audit.capture(agent_self.__class__.__name__, arguments) as entry:
                    result = await original_tool(agent_self, handler, tool_name, arguments)
                    memory_audit.complete(entry, result)
            else:
                result = await original_tool(agent_self, handler, tool_name, arguments)
            rec = _LAST_CALL.get()
            if rec is not None:
                rec.tool_results.append(summarize_tool_result(tool_name, arguments, result))
            if recorder.tool_log is not None and agent_self.__class__.__name__ == "MemoryAgent":
                # phase 9.1.1 (D-P91-3): what the memory model actually passed and got back -- secrets scrubbed,
                # anything the profile rules call sensitive (phone, ID, health...) hidden
                try:
                    returned = json.loads(result)
                except (TypeError, ValueError):
                    returned = result
                recorder.tool_log.append({"round": recorder.round, "call_no": rec.call_no if rec else None,
                                          "tool": tool_name, "arguments": redact_for_log(arguments, recorder.scrub),
                                          "result": redact_for_log(returned, recorder.scrub)})
            return result

        BaseAgent.client_factory = staticmethod(factory)
        BaseAgent._create_completion = tagged
        BaseAgent.run_async = tracked_run
        BaseAgent._call_local_tool = summarized_tool
        from ai.aux_llm import AuxLLM
        self._saved_aux = AuxLLM.complete_json
        original_aux = AuxLLM.complete_json

        def tagged_aux(aux_self, messages):
            token = _AGENT.set(f"AuxLLM:{aux_self.config.purpose}")
            try:
                return original_aux(aux_self, messages)
            finally:
                _AGENT.reset(token)
        AuxLLM.complete_json = tagged_aux
        self.agents = {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(),
                       "performer": PerformerAgent()}
        memory = self.agents["memory"]
        real_memory_run = memory.run

        def memory_run(user_message, call_context=None, **kw):
            if self.memory_script is not None:
                return self._run_memory_script(memory, call_context)
            if no_memory:
                # "what if the model never stores anything": the memory agent declines every job
                return {"content": json.dumps({"should_save": False, "reason": "disabled"})}
            return real_memory_run(user_message, call_context=call_context, **kw)
        memory.run = memory_run
        self.quiet_hours = quiet_hours
        self._write_proactive_config()
        self.wechat = LocalWeChat(image_dir=workdir / "sent_images" if self.image_mock else None)
        holder: dict[str, Any] = {}
        self.scheduler = ProactiveScheduler(ProactiveAgent(), self.agents["chat"], self.agents["performer"],
                                            send_messages=lambda m, e=None: holder["rt"].send_proactive(m, e),
                                            config_path=workdir / "proactive.json")
        self.runtime = VioletRuntime(self.wechat, db, self.agents, proactive_scheduler=self.scheduler,
                                  sender=MessageSender(self.wechat, db, simulate_typing=False, sleep=lambda s: None),
                                  vision=lambda path: "（测试工具不处理图片）")
        holder["rt"] = self.runtime
        if self.image_mock:
            # last, so that nothing can fail after them without close() undoing them
            from ai.images.router import reset_runtime_state
            reset_runtime_state()                 # this session's picture health starts clean (no inherited circuit)
            self._image_restore = lock_image_adapters()
            self.network_guard = NetworkGuard([os.environ.get("LLM_API_URL", "")] if real else []).install()
        self._no_instant_budget_retries()
        self.runtime.start_worker()                           # turn worker + memory worker only

    def _no_instant_budget_retries(self) -> None:
        """Test tool only (phase 7 B.10.1): a memory job refused by this session's budget is marked failed at
        once. Violet's MemoryWorker would retry it (VIOLET_MEMORY_RETRY_BASE_SECONDS is 0 here), but a budget
        refusal cannot recover inside the session; Violet's own retry policy is not changed."""
        worker = getattr(self.runtime, "memory_worker", None)
        if worker is None:
            return
        process = worker._process

        def process_once(job):
            process(job)
            row = self.db.fetch_one("SELECT status, last_error FROM memory_jobs WHERE id = ?", (job["id"],))
            if row and row["status"] == "pending" and (row["last_error"] or "").startswith("BudgetExceeded"):
                self.db.update_memory_job(job["id"], status="failed", finished_at=self.db.now(), last_error=(
                    f"{row['last_error']} [test tool: a budget refusal is not retried]")[:500])
        worker._process = process_once

    def _write_proactive_config(self) -> None:
        cfg = json.loads((PROJECT / "proactive_config.json").read_text(encoding="utf-8"))
        if self.quiet_hours == "off":
            # start == end is an empty window for the scheduler: never quiet, whatever the scenario time
            cfg["quiet_hours"] = {"start": "00:00", "end": "00:00"}
        cfg["allowed_tasks"] = {k: {**v, "enabled": False} for k, v in (cfg.get("allowed_tasks") or {}).items()}
        (self.workdir / "proactive.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def use_clock(self, clock: ScenarioClock) -> None:
        """Switch Violet to a scenario clock (phase 7 B.3). The quiet-hours window of --quiet-hours off is
        recomputed against it; the scheduler re-reads its config on every tick."""
        uninstall_clock(self._saved_clock)
        self.clock = clock
        self._saved_clock = install_clock(clock)
        self._write_proactive_config()

    def _replay_response(self, **kwargs):
        """Offline answer during a replay: Chat gets the recorded reply, Intent a plain chat route.
        Any other model call means the replay is not reproducing the recorded round -> fail loudly."""
        agent = _AGENT.get()
        model = str(kwargs.get("model"))
        if agent == "IntentAgent":
            return MockLLM._completion(json.dumps({
                "route": "chat", "should_reply": True, "user_intent": "replay", "emotion": "neutral",
                "silent_reason": None, "active_task_id": None,
                "task": {"exists": False, "name": None, "description": None}}), model)
        if agent == "ChatAgent" and self.replay_reply is not None:
            return MockLLM._completion(self.replay_reply, model)
        raise ReplayError(f"unexpected {agent} model call during a replay")

    def _run_memory_script(self, memory, call_context) -> dict[str, Any]:
        """Apply the plan's memory actions through MemoryAgent's own tool handlers (same validation)."""
        from ai.agents.base_agent import _CALL_CONTEXT
        token = _CALL_CONTEXT.set(dict(call_context or {}))
        results = []
        try:
            for step in self.memory_script or []:
                result = memory._handlers[step["tool"]](dict(step["args"]))
                results.append(result)
                # kept for the caller (a replay compares them with the recorded results); errors raised in
                # the memory worker would only become job retries
                try:
                    parsed = json.loads(result) if isinstance(result, str) else result
                except ValueError:
                    parsed = {"raw": str(result)[:200]}
                self.memory_script_results.append({"tool": step["tool"], "result": parsed})
        finally:
            _CALL_CONTEXT.reset(token)
        return {"content": json.dumps({"should_save": bool(results), "scripted": True}, ensure_ascii=False)}

    def _quiet(self):
        import contextlib
        import io
        if self.verbose:
            return contextlib.nullcontext()
        buffer = io.StringIO()
        self.pipeline_log.append(buffer)              # type: ignore[arg-type]
        return contextlib.redirect_stdout(buffer)

    def say(self, text: str, round_no: int | None = None, timeout: float = 600) -> dict[str, Any]:
        text = text.lstrip("﻿")
        self.recorder.start_round(round_no)
        self.wechat.current_round = round_no
        marks = self._image_marks()
        images_before = len(self.wechat.images)
        sent_before = len(self.wechat.sent)
        calls_before = len(self.recorder.calls)
        runs_before = len(self.recorder.runs)
        refused_before = len(self.recorder.refused)
        jobs_mark, inbound_mark = self._top_id("memory_jobs"), self._top_id("inbound_messages")
        started = time.monotonic()
        with self._quiet():
            self.runtime.ingest_text(text)
            self.runtime.wait_idle(timeout)
            replied = time.monotonic()
            self.runtime.wait_memory_idle(timeout)
        calls = self.recorder.calls[calls_before:]
        jobs = self.memory_job_facts(jobs_mark)
        refused = self.recorder.refused[refused_before:]
        turns = [dict(r) for r in self.db.fetch_all("SELECT id, status, error FROM inbound_messages WHERE id > ? "
                                                     "ORDER BY id", (inbound_mark,))]
        return {"user": text, "violet": [m["text"] for m in self.wechat.sent[sent_before:]],
                "reply_latency_ms": int((replied - started) * 1000), "calls": [call_detail(c) for c in calls],
                "runs": [dict(r) for r in self.recorder.runs[runs_before:]],
                "images": self._image_facts(marks, self.wechat.images[images_before:]),
                "tokens": sum(c.billed for c in calls),
                "errors": [error_label(c) for c in calls if not c.ok],
                "memory_jobs": jobs,
                "background_complete": all(j["status"] == "done" for j in jobs),
                "execution_errors": execution_errors(jobs, refused, turns, self.recorder.scrub)}

    def _top_id(self, table: str) -> int:
        return int(self.db.fetch_one(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}")["m"])

    def memory_job_facts(self, since_id: int) -> list[dict[str, Any]]:
        """Final state of the memory jobs created after `since_id`: status, attempts and the error type."""
        from ai.memory_jobs import job_outcome
        rows = self.db.fetch_all("SELECT id, status, attempts, last_error, progress FROM memory_jobs WHERE id > ? "
                                 "ORDER BY id", (since_id,))
        outcomes = {r["id"]: job_outcome(r) for r in rows}
        return [{"job_id": r["id"], "status": r["status"], "attempts": r["attempts"],
                 # phase 9.4 (r1): how far it got -- complete / partial (+ the writes) / interrupted / unprocessed
                 "outcome": outcomes[r["id"]]["state"], "budget_interrupted": outcomes[r["id"]]["budget_interrupted"],
                 "writes": outcomes[r["id"]]["writes"],
                 "error_type": (r["last_error"] or "").split(":", 1)[0] or None if r["status"] != "done" else None,
                 "error": self.recorder.scrub(r["last_error"] or "")[:300] or None if r["status"] != "done" else None,
                 "budget_not_retried": "[test tool: a budget refusal is not retried]" in (r["last_error"] or "")}
                for r in rows]

    def _image_marks(self) -> dict[str, int]:
        def top(table: str) -> int:
            try:
                return int(self.db.fetch_one(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}")["m"])
            except Exception:
                return 0
        return {t: top(t) for t in ("image_tasks", "image_calls", "media_assets", "outbound_messages")}

    def _image_facts(self, marks: dict[str, int], sent: list[dict[str, Any]]) -> dict[str, Any]:
        """What happened to pictures in this round, from the database and the local sender: ids, codes,
        hashes -- never prompts or captions."""
        db = self.db
        tasks = [{"task_id": r["task_id"], "kind": r["kind"], "status": r["status"], "error_code": r["error_code"],
                  "entry_id": r["entry_id"], "scene_kind": (json.loads(r["scene"]) or {}).get("kind") if r["scene"] else None,
                  "output_asset_ids": json.loads(r["output_asset_ids"] or "[]")}
                 for r in db.fetch_all("SELECT * FROM image_tasks WHERE id > ? ORDER BY id", (marks["image_tasks"],))]
        calls = [{"entry_id": r["entry_id"], "status": r["status"], "accepted": r["accepted"], "error_code": r["error_code"]}
                 for r in db.fetch_all("SELECT * FROM image_calls WHERE id > ? ORDER BY id", (marks["image_calls"],))]
        assets = [{"asset_id": r["id"], "direction": r["direction"], "media_kind": r["media_kind"],
                   "ai_generated": bool(r["ai_generated"]), "provider_id": r["provider_id"], "model_id": r["model_id"],
                   "sha256": r["sha256"], "file_size": r["file_size"], "status": r["status"], "send_status": r["send_status"],
                   "scene_kind": r["scene_kind"], "life_event_id": r["life_event_id"]}
                  for r in db.fetch_all("SELECT * FROM media_assets WHERE id > ? ORDER BY id", (marks["media_assets"],))]
        outbound = [{"id": r["id"], "status": r["status"], "asset": r["content"], "has_error": bool(r["error"])}
                    for r in db.fetch_all("SELECT * FROM outbound_messages WHERE id > ? AND message_type = 'image' ORDER BY id",
                                          (marks["outbound_messages"],))]
        return {"provider": "mock" if self.image_mock else "off", "tasks": tasks, "calls": calls, "assets": assets,
                "outbound": outbound, "sent": [dict(x) for x in sent]}

    def proactive_tick(self, make_due: bool = True) -> dict[str, Any]:
        """Let the proactive scheduler decide once, as if its next time had come. make_due=False (phase 8.9.6):
        one ordinary check, exactly as the scheduler thread would run it now -- its own next time stays."""
        if make_due:
            self.db.set_proactive_setting("next_proactive_at", (datetime.fromisoformat(self.db.now())
                                                                - timedelta(minutes=1)).isoformat())
        sent_before, events_before = len(self.wechat.sent), len(self.db.list_proactive_events(limit=1000))
        due_reason = self.scheduler.status_snapshot()["due_reason"]          # before the tick changes it
        with self._quiet():
            self.scheduler.tick()
        events = self.db.list_proactive_events(limit=1000)
        event = events[0] if len(events) > events_before else None
        return {"due_reason": due_reason,
                "event": {k: event.get(k) for k in ("status", "reason")} if event else None,
                "sent": [m["text"] for m in self.wechat.sent[sent_before:]]}

    def advance(self, hours: float) -> dict[str, Any]:
        """Let `hours` pass on the scenario clock and report what time alone changed."""
        return self.advance_to(self.clock.now().astimezone(timezone.utc) + timedelta(hours=hours))

    def advance_to(self, when: datetime) -> dict[str, Any]:
        """Move the scenario clock to `when`. History stays as stored; the effects of the passing time are
        applied by Violet's own code (situations expire, planned events whose time passed become unknown)."""
        from ai.profile_store import Profile
        situations = Profile(self.db).situations
        start = self.clock.now()
        active = {s["id"]: s for s in situations.current(include_disabled=True)}
        events = {r["id"]: (r["progress"], r["status"]) for r in self.db.fetch_all(
            "SELECT id, progress, status FROM event_memories WHERE status != 'deleted'")}
        end = self.clock.advance_to(when)
        still = {s["id"] for s in situations.current(include_disabled=True)}
        self.db.refresh_event_states()
        changed = []
        for r in self.db.fetch_all("SELECT id, progress, status FROM event_memories WHERE status != 'deleted'"):
            if r["id"] in events and events[r["id"]] != (r["progress"], r["status"]):
                changed.append({"id": r["id"], "before": list(events[r["id"]]), "after": [r["progress"], r["status"]]})
        elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
        return {"from": start.isoformat(), "to": end.isoformat(), "hours": round(elapsed.total_seconds() / 3600, 3),
                "situations_expired": [{"id": i, "kind": s["kind"], "value": s["value"], "expires_at": s["expires_at"]}
                                       for i, s in active.items() if i not in still],
                "events_changed": changed}

    def state(self) -> dict[str, Any]:
        return {
            "facts": self.db.fetch_all("SELECT fact_key, value, status FROM user_profile_facts ORDER BY id"),
            "situations": self.db.fetch_all("SELECT kind, value, expires_at FROM user_situations ORDER BY id"),
            "memories": self.db.fetch_all("SELECT id, content, kind, progress, status FROM event_memories ORDER BY id"),
            "commitments": self.db.fetch_all("SELECT content, due_at, status FROM commitments ORDER BY id"),
            "life_events": self.db.fetch_all("SELECT content, occurred_at, share_count FROM life_events ORDER BY id"),
        }

    def close(self) -> list[str]:
        """Stop the workers, undo the patches and (if owned) close the database. Idempotent.

        Returns the problems found (threads still running, close errors) instead of raising, so a
        failing close never hides the error that ended the session."""
        if self.closed:
            return []
        self.closed = True
        problems: list[str] = []
        from ai.agents.base_agent import BaseAgent
        from ai.aux_llm import AuxLLM
        try:
            self.runtime.stop(timeout=5)
        except Exception as exc:
            problems.append(f"runtime.stop failed: {exc.__class__.__name__}")
        threads = [("turn worker", getattr(self.runtime, "_worker_thread", None)),
                   ("memory worker", getattr(getattr(self.runtime, "memory_worker", None), "_thread", None))]
        for name, thread in threads:
            if thread is not None and thread.is_alive():
                problems.append(f"{name} still running after stop")
        (BaseAgent.client_factory, BaseAgent._create_completion, BaseAgent.run_async,
         BaseAgent._call_local_tool) = self._saved
        AuxLLM.complete_json = self._saved_aux
        uninstall_clock(self._saved_clock)
        self._saved_clock = []
        if self.network_guard is not None:
            self.network_guard.uninstall()
        if self._image_restore is not None:
            self._image_restore()
            self._image_restore = None
        if self.owns_db:
            if problems:
                problems.append("database left open because a worker is still running")
            else:
                try:
                    self.db.close()
                except Exception as exc:
                    problems.append(f"database close failed: {exc.__class__.__name__}")
        return problems


def write_report(name: str, data: dict[str, Any], recorder: CallRecorder, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or OUTPUT_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    text = recorder.scrub(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    path.write_text(text, encoding="utf-8")
    return path


def _remove_readonly(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_owned_tree(workdir: Path) -> None:
    """Delete everything, the ownership marker last: while any file is still locked the marker stays,
    so a later retry can still prove the directory belongs to this run."""
    error: OSError | None = None
    for child in list(workdir.iterdir()):
        if child.name == OWNER_MARKER:
            continue
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, onerror=_remove_readonly)
            else:
                try:
                    child.unlink()
                except PermissionError:
                    os.chmod(child, stat.S_IWRITE)
                    child.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            error = exc
    if error is not None:
        raise error
    (workdir / OWNER_MARKER).unlink(missing_ok=True)
    workdir.rmdir()


def cleanup(workdir: Path, attempts: int = 10, delay: float = 0.3) -> None:
    """Remove a test directory created by THIS process. Raises IsolationError for anything else and
    CleanupError (listing what is left) when files stay locked -- never fails silently."""
    workdir = Path(workdir).resolve()
    if not (workdir.name.startswith("violet-testchat-") or (workdir / "empty.env").exists()):
        raise IsolationError(f"refusing to delete {workdir}")
    if not created_this_run(workdir):
        raise IsolationError(f"refusing to delete {workdir}: it was not created by this run")
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            _remove_owned_tree(workdir)
        except FileNotFoundError:
            pass
        except OSError as exc:              # Windows: a file is still open somewhere
            last_error = exc
        if not workdir.exists():
            _CREATED.pop(workdir, None)
            return
        if attempt + 1 < attempts:
            time.sleep(delay)
    left = sorted(str(p.relative_to(workdir)) for p in workdir.rglob("*"))[:20]
    raise CleanupError(f"could not delete {workdir} ({last_error.__class__.__name__ if last_error else 'unknown'}); "
                       f"left: {left}")


def finish_session(session, keep: bool, out: Callable[[str], None] = print) -> bool:
    """Close the session and remove its directory. Never raises; returns False when cleanup failed."""
    ok = True
    try:
        problems = session.close()
    except Exception as exc:                 # close() reports instead of raising; this is a last resort
        problems = [f"close failed: {exc.__class__.__name__}"]
    for problem in problems:
        out(f"清理提示：{problem}")
    if keep:
        out(f"测试数据目录已保留：{session.workdir}")
        return ok
    if not created_this_run(session.workdir):
        out(f"测试数据目录不是本次创建的，未删除：{session.workdir}")
        return ok
    try:
        cleanup(session.workdir)
        out("测试数据目录已删除。")
    except (CleanupError, IsolationError, OSError) as exc:
        ok = False
        out(f"清理失败（请手动检查）：{exc}")
    return ok


def list_leftovers(exclude: Path | None = None) -> list[Path]:
    """Test directories left by earlier runs. Listed only -- never deleted automatically."""
    root = Path(tempfile.gettempdir())
    exclude = Path(exclude).resolve() if exclude else None
    found = []
    for path in sorted(root.glob("violet-testchat-*")):
        if path.is_dir() and path.resolve() != exclude and path.resolve() not in _CREATED:
            found.append(path)
    return found
