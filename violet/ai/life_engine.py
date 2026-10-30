"""Low-frequency life simulation for Violet (phase 4).

* run_day(now) is called by the scheduler; it does work at most ONCE per local
  day (life_engine_runs.run_key = date, claimed atomically), so restarts never
  repeat a day. After downtime only "today" is considered: no backfilled diary.
* A seeded RNG (salt + date) makes each day's decision reproducible.
* Budget: at most weekly_max_events per ISO week, no minimum; most days
  nothing happens. At most max_active_threads threads are active.
* Content comes from persona/life_config.json (editable), not from code.
* Plans are never completed by time passing: an overdue plan is resolved only
  by a recorded completion event, or abandoned after a while (with a revision).
* Every proposal goes through ConsistencyGuard before it is stored; a rejected
  proposal is flagged and nothing is written.
* No LLM is involved by default (TemplateProposer). life_config.generation.mode
  = "llm" switches the wording of progress steps to LLMProposer
  (ai/life_proposer_llm.py); its output is validated the same way.
* The engine is driven by ai/life_scheduler.py (its own thread), never by the
  proactive scheduler, and it never sends anything.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any, Protocol

from ai.consistency import ConsistencyGuard
from ai.life_store import LifeStore
from ai.memory_model import parse_time
from ai.persona import PersonaCore, PersonaDynamics, life_config
from ai.settings import env_bool, now_local

logger = logging.getLogger("violet.life")


class Proposer(Protocol):
    def new_thread(self, interest_key: str, rng: random.Random, used_items: set[str]) -> tuple[str, str] | None: ...
    def progress(self, thread: dict[str, Any], rng: random.Random, exclude: set[str]) -> str | None: ...
    def finish(self, thread: dict[str, Any]) -> str: ...
    def plan(self, thread: dict[str, Any]) -> str: ...


class TemplateProposer:
    """Mundane content from life_config.json -- no drama, no invented background."""

    def __init__(self, config: dict[str, Any]):
        self.interests = config.get("interests", {})

    def new_thread(self, interest_key, rng, used_items):
        spec = self.interests.get(interest_key)
        if not spec:
            return None
        items = [i for i in spec.get("items", []) if i not in used_items]
        if not items:
            return None
        item = rng.choice(items)
        return spec["thread_title"].format(item=item), item

    def progress(self, thread, rng, exclude):
        spec = self.interests[thread["interest_key"]]
        options = [t.format(item=thread["item"]) for t in spec["progress"]]
        options = [o for o in options if o not in exclude]
        return rng.choice(options) if options else None

    def finish(self, thread):
        return self.interests[thread["interest_key"]]["finish"].format(item=thread["item"])

    def plan(self, thread):
        return self.interests[thread["interest_key"]]["plan"].format(item=thread["item"])

    def steps_to_finish(self, thread) -> int:
        return int(self.interests[thread["interest_key"]].get("steps_to_finish", 3))


class LifeEngine:
    def __init__(self, db, config: dict[str, Any] | None = None, core: PersonaCore | None = None,
                 proposer: Proposer | None = None):
        self.db = db
        self.config = config or life_config()
        self.core = core or PersonaCore.load(db)
        self.store = LifeStore(db)
        self.guard = ConsistencyGuard(db, self.core, self.config)
        self.dynamics = PersonaDynamics(db, self.config)
        if proposer is None:
            from ai.life_proposer_llm import make_proposer
            proposer = make_proposer(self.config, db, self.core)
        self.proposer = proposer

    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True)) and env_bool("VIOLET_LIFE_ENGINE", True)

    # ------------------------------------------------------------------
    def run_day(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or now_local()
        run_key = now.date().isoformat()
        if not self.enabled():
            return {"outcome": "disabled", "run_key": run_key}
        if getattr(self.core, "is_ai", False):
            # 2026-10-30: an AI that knows it is one has no invented days; its "life" is its real records
            return {"outcome": "ai_persona", "run_key": run_key}
        if now.hour < int(self.config.get("run_after_hour", 21)):
            return {"outcome": "too_early", "run_key": run_key}     # the day is not over yet; not claimed
        if not self.store.claim_run(run_key):
            return {"outcome": "already_ran", "run_key": run_key}
        # the salt is a fixed random-seed string, not a name: kept from before the rename so seeded days stay the same
        rng = random.Random(f"{self.config.get('seed_salt', 'eve')}:{run_key}")
        try:
            result = self._run(now, rng)
        except Exception as exc:
            logger.exception("life engine run %s failed", run_key)
            result = {"outcome": "error", "error": f"{exc.__class__.__name__}: {exc}"}
        result["run_key"] = run_key
        self.store.finish_run(run_key, result["outcome"], result)
        return result

    def _week_count(self, now: datetime) -> int:
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return len([e for e in self.store.events(("happened",), since=week_start.isoformat(), until=now.isoformat())
                    if e["source"].startswith("life_engine")])

    def _run(self, now: datetime, rng: random.Random) -> dict[str, Any]:
        abandoned = self._abandon_stale_plans(now)
        if self._week_count(now) >= int(self.config.get("weekly_max_events", 3)):
            return {"outcome": "quiet_weekly_budget", "abandoned_plans": abandoned}
        if rng.random() >= float(self.config.get("daily_event_probability", 0.35)):
            return {"outcome": "quiet", "abandoned_plans": abandoned}
        active = self.store.threads(["active"])
        can_start = len(active) < int(self.config.get("max_active_threads", 2))
        if can_start and (not active or rng.random() < float(self.config.get("new_thread_probability", 0.25))):
            started = self._start_thread(now, rng, active)
            if started:
                return {"abandoned_plans": abandoned, **started}
        if not active:
            return {"outcome": "quiet_no_thread", "abandoned_plans": abandoned}
        thread = rng.choice(active)
        return {"abandoned_plans": abandoned, **self._advance(thread, now, rng)}

    def _occurred_at(self, now: datetime, rng: random.Random) -> datetime:
        candidate = now.replace(hour=rng.randint(9, 21), minute=rng.choice((0, 15, 30, 45)), second=0, microsecond=0)
        return self._clear_of_daily(min(candidate, now - timedelta(minutes=int(self.config.get("event_duration_minutes", 60)))),
                                    now)

    def _clear_of_daily(self, occurred: datetime, now: datetime) -> datetime:
        """Phase 8.9: move off the small daily items (ai/daily_life.py) of that day instead of being rejected for a
        time conflict. No random draw, so days without daily items keep exactly their old times."""
        from ai.daily_life import SOURCE
        minutes = timedelta(minutes=int(self.config.get("event_duration_minutes", 60)))
        day = occurred.date().isoformat()
        daily = [e for e in self.store.events(("happened",)) if e["source"] == SOURCE and e["occurred_at"][:10] == day]
        for _ in range(len(daily) + 1):
            clash = next((e for e in daily if parse_time(e["occurred_at"]) < occurred + minutes
                          and occurred < parse_time(e["occurred_at"]) + timedelta(minutes=int(e["duration_minutes"] or 60))),
                         None)
            if clash is None:
                return occurred
            start = parse_time(clash["occurred_at"])
            after = start + timedelta(minutes=int(clash["duration_minutes"] or 60))
            occurred = after if after + minutes <= now else start - minutes
        return occurred

    def _store_event(self, *, thread: dict[str, Any] | None, event_type: str, content: str, occurred: datetime,
                     now: datetime, uid: str, plan_id: int | None = None, completes_plan: bool = False,
                     source: str = "life_engine:template", source_detail: Any = None) -> dict[str, Any]:
        proposal = {
            "thread_id": thread["id"] if thread else None, "plan_id": plan_id, "event_type": event_type, "content": content,
            "occurred_at": occurred.isoformat(), "duration_minutes": int(self.config.get("event_duration_minutes", 60)),
            "source": source, "source_detail": source_detail, "completes_plan": completes_plan,
        }
        violations = self.guard.check_event(proposal, now.isoformat())
        if violations:
            for v in violations:
                self.store.flag("life_event_proposal", uid, v.check, f"{v.detail} | {content}")
            return {"outcome": "rejected", "violations": [v.check for v in violations], "content": content}
        event_id = self.store.insert_event(uid=uid, thread_id=proposal["thread_id"], plan_id=plan_id, event_type=event_type,
                                           content=content, occurred_at=proposal["occurred_at"],
                                           duration_minutes=proposal["duration_minutes"], source=proposal["source"],
                                           source_detail=source_detail)
        if event_id is None:
            return {"outcome": "duplicate_uid", "uid": uid}
        if thread:
            self.store.bump_thread(thread["id"], proposal["occurred_at"])
        return {"outcome": "event", "event_id": event_id, "event_type": event_type, "content": content}

    def _start_thread(self, now: datetime, rng: random.Random, active: list[dict[str, Any]]) -> dict[str, Any] | None:
        busy_interests = {t["interest_key"] for t in active}
        interests = [k for k in self.core.interest_keys if k not in busy_interests and k in self.config.get("interests", {})]
        if not interests:
            return None
        interest = rng.choice(interests)
        used = {t["item"] for t in self.store.threads() if t["interest_key"] == interest and t["item"]}
        proposal = self.proposer.new_thread(interest, rng, used)
        if not proposal:
            return None
        title, item = proposal
        occurred = self._occurred_at(now, rng)
        thread_key = f"{interest}:{item}"
        thread_id = self.store.create_thread(thread_key, interest, title, item, occurred.isoformat())
        if thread_id is None:
            return None
        thread = self.store.thread(thread_id)
        result = self._store_event(thread=thread, event_type="thread_started", content=f"开始{title}", occurred=occurred,
                                   now=now, uid=f"{thread_key}:start")
        if result["outcome"] != "event":
            self.store.set_thread_status(thread_id, "cancelled", "start event rejected by consistency guard", "life_engine")
        return {**result, "thread_id": thread_id}

    def _advance(self, thread: dict[str, Any], now: datetime, rng: random.Random) -> dict[str, Any]:
        uid_base = f"{thread['thread_key']}:{now.date().isoformat()}"
        occurred = self._occurred_at(now, rng)
        open_plans = self.store.plans(["planned", "active"], thread_id=thread["id"])
        # phase 8.9.13: only the engine's own plans ("想把…读完/听一遍") are what finishing the thread completes. A plan
        # from elsewhere ("周末想去书店挑一本新书", a seeded test plan) was marked done by "读完了《…》" in a 14-day
        # free_chat run; such plans keep their own course (left open, abandoned when long overdue).
        overdue = [p for p in open_plans if parse_time(p["planned_for"]) <= now and p.get("source") == "life_engine"]
        resolution = self.config.get("plan_resolution", {})
        if overdue and rng.random() < float(resolution.get("complete_probability", 0.6)):
            plan = overdue[0]
            result = self._store_event(thread=thread, event_type="thread_completed", content=self.proposer.finish(thread),
                                       occurred=occurred, now=now, uid=f"{uid_base}:finish", plan_id=plan["id"],
                                       completes_plan=True)
            if result["outcome"] == "event":
                self.store.set_plan_status(plan["id"], "completed", "finished, recorded by an event", "life_engine",
                                           resolution_event_id=result["event_id"])
                self.store.set_thread_status(thread["id"], "completed", "finished", "life_engine", at=occurred.isoformat())
                self._after_completion(thread)
            return result
        window = timedelta(days=float(self.config.get("duplicate_window_days", 14)))
        recent = {e["content"] for e in self.store.events(("happened",), thread_id=thread["id"],
                                                          since=(now - window).isoformat())}
        content = self.proposer.progress(thread, rng, recent)
        detail = getattr(self.proposer, "last_detail", None)
        if content is None:
            return {"outcome": "quiet_nothing_new", "thread_id": thread["id"], "proposer": detail}
        result = self._store_event(thread=thread, event_type="progress", content=content,
                                   occurred=occurred, now=now, uid=uid_base,
                                   source=getattr(self.proposer, "source", "life_engine:template"), source_detail=detail)
        steps = (self.store.thread(thread["id"]) or thread)["progress_steps"]
        if result["outcome"] == "event" and not open_plans and steps >= self.proposer.steps_to_finish(thread):
            planned_for = (now + timedelta(days=rng.randint(2, 5))).replace(hour=20, minute=0, second=0, microsecond=0)
            result["plan_id"] = self.store.create_plan(thread["id"], self.proposer.plan(thread), planned_for.isoformat())
        return result

    def _abandon_stale_plans(self, now: datetime) -> list[int]:
        days = float(self.config.get("plan_resolution", {}).get("abandon_after_days_overdue", 10))
        abandoned = []
        for plan in self.store.plans(["planned", "active"]):
            if parse_time(plan["planned_for"]) + timedelta(days=days) < now:
                self.store.set_plan_status(plan["id"], "abandoned", f"not done {int(days)} days after it was planned",
                                           "life_engine")
                abandoned.append(plan["id"])
        return abandoned

    def _after_completion(self, thread: dict[str, Any]) -> None:
        events = [e["id"] for e in self.store.events(("happened",), thread_id=thread["id"])]
        label = next((i["label"] for i in self.core.data.get("long_term_interests", []) if i["key"] == thread["interest_key"]),
                     thread["interest_key"])
        self.dynamics.propose(f"interest.{thread['interest_key']}.recent", "temporary_interest",
                              f"这阵子对{label}兴致比较高", f"刚{thread['title']}", related_event_ids=events)
