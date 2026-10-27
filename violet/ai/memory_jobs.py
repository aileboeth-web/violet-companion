"""Background memory consolidation (MemoryAgent off the reply's critical path).

Lifecycle of a ``memory_jobs`` row (one per user turn, unique turn_id):
    waiting_reply -> pending (reply sent) -> running -> done
                                                  \\-> pending (retry, backoff) -> ... -> failed
Recovery at startup: running -> pending; waiting_reply older than
VIOLET_MEMORY_WAIT_REPLY_SECONDS -> pending (reply unknown).

Memory writes are idempotent per turn (event_memories.source_turn_id,
commitments.source_turn_id, user_situations.source_turn_id), so a retried job does not create duplicates.

Phase 9.4 (real run cys-p94-sched-r1: set_situation succeeded, then the next model call was refused and the job was
just "failed"): memory_jobs.progress records, per attempt, every write that really succeeded and why the attempt
stopped ("complete" / "error" / "budget" -- a budget refusal only exists in the test tools). A retry is told what is
already written, so it only adds what is still missing. job_outcome() reads it back:
    unprocessed  nothing attempted yet                 partial      something written, the work did not finish
    complete     the model finished (status done)      interrupted  attempted, nothing written, not finished
plus budget_interrupted when the last stop was a budget refusal. The status column keeps its old meaning.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta
from typing import Any

from ai.memory_model import parse_time
from ai.settings import env_float, env_int

logger = logging.getLogger("violet.memory_jobs")


def build_memory_input(user_message: str, assistant_reply: str | None) -> str:
    text = f"用户说：{user_message}"
    if assistant_reply:
        text += f"\nViolet 实际回复：{assistant_reply}"
        if assistant_reply.strip() == _fallback_reply():
            # phase 9.2.1 (p92-tools-1 #11): Chat failed and only the canned line went out -- nothing was answered or
            # looked up this turn, so nothing the user asked about (a condition, a question) may be taken as settled
            text += "\n（这是系统兜底消息：Violet 这一轮没能正常回复，没有回答也没有查到任何东西；不要据此推断任何事。）"
    return text


def _fallback_reply() -> str:
    from ai.settings import env_str
    from violet_runtime import DEFAULT_FALLBACK_REPLY
    return (env_str("VIOLET_FALLBACK_REPLY", DEFAULT_FALLBACK_REPLY) or "").strip()


def read_progress(job: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(job.get("progress") or "{}")
    except (TypeError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"writes": list(data.get("writes") or []), "stops": list(data.get("stops") or [])}


def _progress_json(progress: dict[str, Any], attempt: int, written: list[dict[str, Any]], stop: str) -> str:
    return json.dumps({"writes": progress["writes"] + [{**w, "attempt": attempt} for w in written],
                       "stops": progress["stops"] + [{"attempt": attempt, "stop": stop, "writes": len(written)}]},
                      ensure_ascii=False)


def job_outcome(job: dict[str, Any]) -> dict[str, Any]:
    """How far a memory job really got (see the module docstring)."""
    progress = read_progress(job)
    last = progress["stops"][-1]["stop"] if progress["stops"] else None
    if job.get("status") == "done":
        state = "complete"
    elif progress["writes"]:
        state = "partial"
    elif not job.get("attempts"):
        state = "unprocessed"
    else:
        state = "interrupted"
    return {"state": state, "budget_interrupted": last == "budget", "writes": progress["writes"],
            "retry_pending": job.get("status") == "pending" and state != "complete"}


class MemoryWorker:
    def __init__(self, db, memory_agent, poll_interval: float = 0.2):
        self.db = db
        self.memory_agent = memory_agent
        self.poll_interval = poll_interval
        self.max_attempts = max(1, env_int("VIOLET_MEMORY_MAX_ATTEMPTS", 3))
        self.retry_base = env_float("VIOLET_MEMORY_RETRY_BASE_SECONDS", 30.0)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_job: int | None = None

    # -- queue ------------------------------------------------------------------
    def enqueue(self, turn_id: str, user_message: str, user_message_id: int | None) -> int | None:
        job_id = self.db.add_memory_job(turn_id, user_message, user_message_id, status="waiting_reply")
        if job_id is None:
            logger.info("memory job for turn %s already exists", turn_id)
        return job_id

    def mark_ready(self, turn_id: str, assistant_reply: str | None) -> None:
        job = self.db.get_memory_job_by_turn(turn_id)
        if not job or job["status"] != "waiting_reply":
            return
        self.db.update_memory_job(job["id"], status="pending", assistant_reply=assistant_reply, next_attempt_at=self.db.now())
        self._wake.set()

    def recover(self) -> int:
        recovered = 0
        for job in self.db.list_memory_jobs(["running"]):
            self.db.update_memory_job(job["id"], status="pending", last_error="interrupted; recovered at startup")
            recovered += 1
        cutoff = parse_time(self.db.now()) - timedelta(seconds=env_float("VIOLET_MEMORY_WAIT_REPLY_SECONDS", 120))
        for job in self.db.list_memory_jobs(["waiting_reply"]):
            created = parse_time(job["created_at"])
            if created and created < cutoff:
                self.db.update_memory_job(job["id"], status="pending", last_error="reply never confirmed")
                recovered += 1
        if recovered:
            logger.warning("memory jobs: %d recovered", recovered)
        return recovered

    # -- processing ------------------------------------------------------------------
    def run_once(self) -> bool:
        job = self.db.claim_next_memory_job()
        if job is None:
            return False
        self._running_job = job["id"]
        try:
            self._process(job)
        finally:
            self._running_job = None
        return True

    def _process(self, job: dict[str, Any]) -> None:
        progress = read_progress(job)
        written: list[dict[str, Any]] = []              # filled by the MemoryAgent run as writes succeed
        call_context = {
            "turn_id": job["turn_id"],
            "user_message": job["user_message"],
            "assistant_reply": job.get("assistant_reply"),
            "user_message_id": job.get("user_message_id"),
            "background_job": True,         # the final text of this run is never read (phase 7 B.2)
            "write_log": written,
            "previous_writes": list(progress["writes"]),
        }
        try:
            self.memory_agent.run(build_memory_input(job["user_message"], job.get("assistant_reply")),
                                  call_context=call_context)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"[:500]
            stop = "budget" if exc.__class__.__name__ == "BudgetExceeded" else "error"
            saved = _progress_json(progress, job["attempts"], written, stop)
            if job["attempts"] >= self.max_attempts:
                logger.error("memory job %s failed permanently after %d attempts (%d writes kept): %s", job["id"],
                             job["attempts"], len(progress["writes"]) + len(written), error)
                self.db.update_memory_job(job["id"], status="failed", last_error=error, finished_at=self.db.now(),
                                          progress=saved)
            else:
                delay = self.retry_base * (2 ** (job["attempts"] - 1))
                next_at = (parse_time(self.db.now()) + timedelta(seconds=delay)).isoformat()
                logger.warning("memory job %s failed (attempt %d, %d writes kept), retry in %.0fs: %s", job["id"],
                               job["attempts"], len(progress["writes"]) + len(written), delay, error)
                self.db.update_memory_job(job["id"], status="pending", last_error=error, next_attempt_at=next_at,
                                          progress=saved)
            return
        # last_error of an earlier failed attempt is kept on purpose (traceability).
        self.db.update_memory_job(job["id"], status="done", finished_at=self.db.now(),
                                  progress=_progress_json(progress, job["attempts"], written, "complete"))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                logger.exception("memory worker iteration failed")
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="violet-memory", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Test helper: no runnable pending job and nothing running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            busy = self._running_job is not None or any(
                (parse_time(j.get("next_attempt_at")) or parse_time(self.db.now())) <= parse_time(self.db.now())
                for j in self.db.list_memory_jobs(["pending", "running"])
            )
            if not busy:
                return True
            time.sleep(0.02)
        return False
