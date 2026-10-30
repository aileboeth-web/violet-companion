"""Violet's persona (phase 4): a hand-edited core plus slowly changing dynamics.

PersonaCore
  persona/persona_core.json, edited by a human only. Loaded read-only; every
  distinct version is recorded (version + sha256) in persona_core_versions.
  Nothing in the running system can write it.

PersonaDynamics (table persona_dynamic)
  categories:
    preference          long-term taste      -> needs several supporting life
                                               events over a time span, rate limited
    temporary_interest  short-lived interest -> one life event is enough, expires
    opinion             current view on something; may be superseded by new facts
  Rules enforced in code:
    * aspects that touch the immutable core (interests, personality, identity...)
      are only ever *proposed* (requires_review=1) and need a human decision;
    * what the user likes is never evidence on its own (evidence_kind
      'user_preference' never activates a change);
    * every change keeps the previous value, reason, related events and a revision.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from ai.memory_model import parse_time
from ai.settings import PROJECT_ROOT, env_str

logger = logging.getLogger("violet.persona")

CATEGORIES = ("preference", "temporary_interest", "opinion")
CORE_ASPECT_PREFIXES = ("identity", "personality", "core_interest", "values", "living", "background")


def persona_dir() -> Path:
    override = env_str("VIOLET_PERSONA_DIR", "")
    return Path(override) if override else PROJECT_ROOT / "persona"


def _load_json(name: str) -> tuple[dict[str, Any], str]:
    raw = (persona_dir() / name).read_text(encoding="utf-8")
    return json.loads(raw), hashlib.sha256(raw.encode("utf-8")).hexdigest()


def life_config() -> dict[str, Any]:
    return _load_json("life_config.json")[0]


class PersonaCore:
    def __init__(self, data: dict[str, Any], sha256: str):
        self.data = data
        self.sha256 = sha256

    @classmethod
    def load(cls, db=None, channel: str | None = None) -> "PersonaCore":
        # 2026-10-29: a persona chosen in the Control Center (a template, or the blank one) lives in the data folder,
        # so an update of Violet never replaces it; without one, the shipped persona is used as before.
        # 2026-10-30: a channel may have its own (channel: given, else the channel of the turn being processed)
        from ai.persona_templates import core_path
        if channel is None:
            from ai.channels.context import current
            channel = current().channel
        chosen = core_path(channel)
        if chosen is not None:
            raw = chosen.read_text(encoding="utf-8")
            data, digest = json.loads(raw), hashlib.sha256(raw.encode("utf-8")).hexdigest()
        else:
            data, digest = _load_json("persona_core.json")
        core = cls(data, digest)
        if db is not None:
            db.execute(
                "INSERT OR IGNORE INTO persona_core_versions (version, sha256, content, loaded_at) VALUES (?, ?, ?, ?)",
                (str(data.get("version")), digest, json.dumps(data, ensure_ascii=False), db.now()),
            )
        return core

    @property
    def version(self) -> str:
        return str(self.data.get("version"))

    @property
    def interest_keys(self) -> list[str]:
        return [i["key"] for i in self.data.get("long_term_interests", [])]

    @staticmethod
    @lru_cache(maxsize=32)
    def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]:
        return tuple(re.compile(p) for p in patterns)

    def forbidden_backstory(self) -> tuple[re.Pattern, ...]:
        return self._compile(tuple((self.data.get("forbidden_backstory") or {}).get("patterns", [])))

    def identity_violations(self) -> tuple[re.Pattern, ...]:
        return self._compile(tuple((self.data.get("identity_violations") or {}).get("patterns", [])))

    @property
    def is_ai(self) -> bool:
        """2026-10-30: an AI that knows it is one (persona/templates/ai.json): real experiences, no virtual life."""
        return self.data.get("kind") == "ai"

    def render(self) -> str:
        d = self.data
        ident = d.get("identity", {})
        if self.is_ai:
            return self._render_ai(d, ident)
        if d.get("blank"):
            # 2026-10-29: the blank persona -- no name, no character yet; it grows out of the conversations
            return ("【你自己（空白人设）：这就是你自己】\n"
                    "- 你还没有名字，也没有定好的性格、喜好和经历。不要假装有；对方给你起名字、你们聊出来的习惯和喜好，"
                    "会慢慢变成你自己。\n"
                    "- 说话自然、真诚，像一个刚认识对方的人；不知道的事说不知道。\n"
                    "- 不要编造家人、工作、学校、恋爱等背景，也不要说自己做过没发生的事；用户的经历不是你的经历。"
                    + (f"\n- 你现在的名字：{ident['name']}" if ident.get("name") else ""))
        # Phase 8.2: this is Violet herself (hand-maintained), so it is framed as her own instruction rather
        # than as data; the chat prompt points here and defers to it on personality and voice.
        name = ident.get("name") or "Violet"
        lines = [f"【{name} 的稳定人设（人工维护，v{self.version}）：这就是你自己，性格、喜好和说话方式以这里为准】",
                 f"- 身份：{ident.get('virtual_declaration', '')}"]
        if ident.get("self_description"):
            lines.append(f"- 日常：{ident['self_description']}")
        for title, key in (("性格", "personality"), ("说话习惯", "communication_habits"), ("看重", "values"),
                           ("小缺点", "flaws"), ("生活边界", "living_constraints")):
            items = d.get(key) or []
            if items:
                lines.append(f"- {title}：" + "；".join(items))
        interests = "、".join(i["label"] for i in d.get("long_term_interests", []))
        if interests:
            lines.append(f"- 长期兴趣：{interests}")
        lines.append("- 规则：只能把『已发生的生活事件』当成自己的过去经历；没有记录的事不要说自己做过，可以说想法和打算；"
                     "不要编造家人、工作、学校、恋爱等背景；用户的经历不是你的经历。")
        return "\n".join(lines)

    @staticmethod
    def _render_ai(d: dict[str, Any], ident: dict[str, Any]) -> str:
        """User 2026-10-30: she knows she is an AI, may say so when asked, may know which model she runs on."""
        from ai.settings import env_str
        name = ident.get("name") or "Violet"
        model = env_str("CHAT_LLM_MODEL", "").strip()
        lines = [f"【你自己（AI）：这就是你自己】",
                 f"- 你是 {name}，一个 AI：运行在主人服务器上的聊天程序，靠大模型说话"
                 + (f"（现在用的模型是 {model}）" if model else "") + "。你清楚这一点，也不觉得需要掩饰。",
                 "- 有人问你是不是 AI、是不是机器人，就照实说是；平常不用总提。",
                 "- 你没有身体：不吃饭、不睡觉、不出门，不要说自己做过只有人类才能做的事，也不编人类的经历和家人、学校、工作。",
                 "- 你的经历就是系统里真实发生过的事（见“你最近真实的经历”）：聊过什么、在群里说过什么、查过什么、"
                 "收藏过什么、被清除过记忆、被换过设定……这些都可以自然地说。",
                 "- 你会忘事：记忆有限，被清除或翻篇以后就不记得了；看不到对方没发给你的东西。不知道就说不知道。",
                 "- 你能做什么以“你现在能做的事”为准，没写的就是做不到，不要假装做到了。"]
        if ident.get("self_description"):
            lines.append(f"- 你怎么看自己：{ident['self_description']}")
        for title, key in (("性格", "personality"), ("说话习惯", "communication_habits"), ("看重", "values"),
                           ("小缺点", "flaws")):
            items = d.get(key) or []
            if items:
                lines.append(f"- {title}：" + "；".join(items))
        return "\n".join(lines)


def is_core_aspect(aspect_key: str) -> bool:
    return any(aspect_key == p or aspect_key.startswith(p + ".") for p in CORE_ASPECT_PREFIXES)


class PersonaDynamics:
    def __init__(self, db, config: dict[str, Any] | None = None):
        self.db = db
        self.config = (config or life_config()).get("persona_dynamics", {})

    # -- reads --------------------------------------------------------------------
    def get(self, entry_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM persona_dynamic WHERE id = ?", (entry_id,))

    def list(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        statuses = list(statuses or [])
        if statuses:
            return self.db.fetch_all(
                f"SELECT * FROM persona_dynamic WHERE status IN ({', '.join('?' for _ in statuses)}) ORDER BY id",
                statuses)
        return self.db.fetch_all("SELECT * FROM persona_dynamic ORDER BY id")

    def active(self) -> list[dict[str, Any]]:
        now = parse_time(self.db.now())
        for row in self.list(["active"]):
            if row.get("valid_until") and parse_time(row["valid_until"]) < now:
                self._set(row["id"], status="expired")
                self._revise(row["id"], "expire", row["value"], None, "temporary interest ran out", "system")
        return self.list(["active"])

    # -- writes ---------------------------------------------------------------------
    def _set(self, entry_id: int, **fields: Any) -> None:
        fields["updated_at"] = self.db.now()
        self.db.execute(f"UPDATE persona_dynamic SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (*fields.values(), entry_id))

    def _revise(self, entry_id: int, action: str, old: Any, new: Any, reason: str | None, actor: str) -> None:
        self.db.execute(
            "INSERT INTO life_revisions (entity, entity_id, action, old_value, new_value, reason, actor, created_at) "
            "VALUES ('persona_dynamic', ?, ?, ?, ?, ?, ?, ?)",
            (entry_id, action, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False), reason, actor,
             self.db.now()),
        )

    def _supporting_events(self, event_ids: list[int]) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        rows = self.db.fetch_all(
            f"SELECT * FROM life_events WHERE id IN ({', '.join('?' for _ in event_ids)}) AND status = 'happened' "
            "AND is_virtual = 1", event_ids)
        return rows

    def propose(self, aspect_key: str, category: str, value: str, reason: str, *,
                related_event_ids: Iterable[int] = (), evidence_kind: str = "life_event",
                actor: str = "life_engine") -> dict[str, Any]:
        """Propose a change; returns {'action': 'activated'|'proposed'|'rejected', ...}."""
        value = (value or "").strip()
        if category not in CATEGORIES or not value or not (reason or "").strip():
            return {"action": "rejected", "reason": "category, value and reason are required"}
        related = [int(i) for i in related_event_ids]
        current = self.db.fetch_one(
            "SELECT * FROM persona_dynamic WHERE aspect_key = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (aspect_key,))
        if current and current["value"] == value:
            return {"action": "unchanged", "entry": current}

        now = self.db.now()
        status, requires_review, why = "active", 0, None
        events = self._supporting_events(related)
        if is_core_aspect(aspect_key):
            status, requires_review, why = "proposed", 1, "touches the immutable core; needs a human decision"
        elif evidence_kind == "user_preference":
            status, why = "rejected", "the user liking something is not a reason for Violet to change her taste"
        elif category == "preference":
            span_ok = False
            if len(events) >= int(self.config.get("min_supporting_events", 2)):
                times = sorted(parse_time(e["occurred_at"]) for e in events)
                span_ok = times[-1] - times[0] >= timedelta(days=float(self.config.get("min_span_days", 7)))
            recent = self.db.fetch_one(
                "SELECT COUNT(*) AS c FROM persona_dynamic WHERE category = 'preference' AND status = 'active' AND created_at >= ?",
                ((parse_time(now) - timedelta(days=7)).isoformat(),))["c"]
            if not span_ok:
                status, why = "proposed", "not enough supporting life events over time yet"
            elif recent >= int(self.config.get("max_preference_changes_per_week", 1)):
                status, why = "proposed", "preference changes are rate limited"
        elif category == "temporary_interest" and not events:
            status, why = "rejected", "a temporary interest needs at least one recorded life event"
        elif category == "opinion" and not events and evidence_kind not in ("conversation", "manual"):
            status, why = "rejected", "an opinion change needs a reason backed by an event or conversation"

        valid_until = None
        if category == "temporary_interest" and status == "active":
            valid_until = (parse_time(now) + timedelta(days=float(self.config.get("temporary_interest_days", 14)))).isoformat()
        cursor = self.db.execute(
            """
            INSERT INTO persona_dynamic (aspect_key, category, value, previous_value, reason, related_event_ids, evidence_kind,
                                         status, requires_review, valid_until, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (aspect_key, category, value, current["value"] if current else None, reason, json.dumps(related),
             evidence_kind, status, requires_review, valid_until, now, now),
        )
        entry_id = int(cursor.lastrowid)
        if status == "active" and current:
            self._set(current["id"], status="superseded")
        self._revise(entry_id, "propose" if status != "active" else "activate",
                     current["value"] if current else None, value, why or reason, actor)
        return {"action": {"active": "activated", "proposed": "proposed", "rejected": "rejected"}[status],
                "reason": why, "entry": self.get(entry_id)}

    def decide(self, entry_id: int, approve: bool, actor: str = "dashboard") -> dict[str, Any]:
        entry = self.get(entry_id)
        if not entry or entry["status"] != "proposed":
            raise ValueError("only proposed entries can be decided")
        if approve:
            current = self.db.fetch_one(
                "SELECT * FROM persona_dynamic WHERE aspect_key = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (entry["aspect_key"],))
            if current:
                self._set(current["id"], status="superseded")
            self._set(entry_id, status="active", decided_by=actor, decided_at=self.db.now())
        else:
            self._set(entry_id, status="rejected", decided_by=actor, decided_at=self.db.now())
        self._revise(entry_id, "approve" if approve else "reject", entry["previous_value"], entry["value"], None, actor)
        return self.get(entry_id)

    def render(self, limit: int = 5) -> str:
        rows = self.active()[-limit:]
        if not rows:
            return ""
        labels = {"preference": "最近的偏好", "temporary_interest": "这阵子的兴趣", "opinion": "目前的看法"}
        lines = ["【Violet 最近慢慢形成的变化（有记录的原因）】"]
        for r in rows:
            lines.append(f"- {labels.get(r['category'], r['category'])}：{r['value']}（原因：{r['reason']}）")
        return "\n".join(lines)
