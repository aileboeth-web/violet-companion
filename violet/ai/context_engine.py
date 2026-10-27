"""Context engine (phase 3): one place that decides what the model sees.

Sections (in priority order, each item carries its source/time/state):
  1. interaction preferences + boundaries      (always; small)
  2. relevant profile facts                    (only when relevant to this turn)
  3. current situation                         (only unexpired; else "unknown")
  4. explicit commitments                      (chat)
  5. recalled memories                          (phase-2 labelling)
  6. legacy core memories                       (old system, unconfirmed)
  7. restricted / cooling topics, avoid topics (phase 2 + preferences)
  8. optional get-to-know-you hint              (chat, at most one)
  9. base info (time; vehicle position is labelled as the car's, not the user's)
The persona prompt stays the agent's first system message (unchanged).
Recent dialogue is returned as real user/assistant turns, never as a system
"summary". Disabled values are masked everywhere; a character budget trims
the lowest-priority items first.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ai.conversation_state import ASK_ABOUT_VIOLET_RE
from ai.memory_model import touches_topic
from ai.interaction import (IDLE_RE, asks_about_eve, follow_up_preference, habit_note, intent_note, is_self_recall,
                            needs_focus, turn_note, waited_note)
from ai.onboarding import OnboardingPlanner, is_important_moment
from ai.profile_schema import CATEGORY_LABELS, PREF_LABELS, PROFILE_KEYS, SITUATION_KINDS, spec_for
from ai.profile_store import Profile, _norm
from ai.settings import env_bool, env_float, env_int, env_str

logger = logging.getLogger("violet.context")

MASK = "[已停用的信息]"
NOW_WORDS = ("在哪", "在干嘛", "在干什么", "现在", "此刻", "在家", "在宿舍", "在学校", "回家", "到家", "出门")
# Phase 7 B.5: the user ASKS where they are / what they are doing (statements such as "回家了" are not questions)
WHERE_QUESTION_WORDS = ("在哪", "哪儿", "哪里", "什么地方")
DOING_QUESTION_WORDS = ("在干嘛", "在干什么", "在做什么", "忙什么", "干嘛呢")
# "你（现在）在干嘛 / 在哪" asks about Violet, not about the user: no user-situation hints for those
ASKS_ABOUT_VIOLET_NOW_RE = re.compile(r"你(?:现在|此刻|今天|最近|这会儿|这会)?(?:在哪|在干嘛|在干什么|在做什么|忙什么|干嘛呢)")
LOCATION_UNKNOWN_LINE = "- 此刻在哪里：不知道（不要根据常住地或生活规律推断；对方让你猜时，只能明确说是猜的）"
ACTIVITY_UNKNOWN_LINE = "- 此刻在做什么：不知道（不要根据生活规律推断）"
KNOWN_LOCATION_FOOTER = ("对方在问自己此刻在哪：以上面的『此刻所在』为准回答。可以说得自然、轻松，不必照抄原话，"
                         "也可以开玩笑，但不要故意猜成和它矛盾的地方。")
# phase 9.4: the user's own word on where they are / what they do / whether they are free outranks the timetable plan
USER_STATED_SOURCES = ("user_stated", "dashboard")
PLAN_ANSWERED_KINDS = ("location", "activity", "availability")
PROACTIVE_KEYS = ("address_name", "interest", "routine.schedule", "identity.status", "boundary")
LIFE_TOPIC_WORDS = ("书", "小说", "读", "看完", "音乐", "歌", "专辑", "听", "笔记", "记录", "写")
RESTRICTED_HEADER = (
    "以下话题现在不适合由你主动提起（事件进展未知或已结束，或你最近已主动提过而对方反应平淡）。"
    "除非用户本轮主动说起，否则不要提起，也不要换个说法追问："
)
ORIGIN_LABELS = {"explicit": "用户亲口说", "dashboard": "用户在管理面板确认"}

# Phase 8.4: "natural" (default) shows Chat what Violet would actually know -- no source tags, dates or admin
# wording, preferences as how to behave, real message times, reminders already sent, how long you have known
# each other. "legacy" renders exactly what earlier phases rendered (kept to re-check recorded runs, 8.3 A/B).
CONTEXT_STYLES = ("natural", "legacy")
PREF_GUIDANCE = {
    ("follow_up_questions", "no"): "平常的分享直接接话或说说你的想法，不用反问；对方问你问题、或者明显想聊下去时照常回应",
    ("follow_up_questions", "less"): "别一句接一句地问；普通分享直接接话就好，真想知道时最多问一句",
    ("reply_length", "short"): "回复短一点",
    ("advice", "only_when_asked"): "对方没问就不给建议",
    ("advice", "no"): "不主动给建议",
    ("humor", "no"): "少开玩笑",
}
NATURAL_ORIGINS = {"explicit": "对方亲口说的", "dashboard": "对方确认过的"}
RECENT_REMINDER_HOURS = 48
# Phase 9.1 (A2): the user asks about something said or done before ("还记得…吗", "上次说的…是什么")
PAST_QUESTION_RE = re.compile(r"(还记得|记不记得|记得吗|记得么|忘了吗|忘记了吗|忘了没)|"
                              r"(上次|之前|以前|那次|那天|说过|提过|告诉过你|跟你说过|聊过).{0,20}(吗|么|呢|[?？]|什么|哪|几)")
NOT_FOUND_LINE = ("- 记忆库这次没有找到和这句话直接相关的记录。这不代表对方没说过：上面的聊天记录里有的以聊天记录为准；"
                  "没有的话别说“你没跟我说过”，也别编细节，记不清就坦白说记不太清，可以请对方再说说。")


def context_style() -> str:
    style = env_str("VIOLET_CONTEXT_STYLE", "natural").strip().lower()
    return style if style in CONTEXT_STYLES else "natural"


def _span(delta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(1, minutes)} 分钟"
    hours = minutes // 60
    return f"约 {hours} 小时" if hours < 48 else f"约 {hours // 24} 天"


def _clock(text: str) -> str:
    return (text or "")[5:16].replace("T", " ")


def _replies(session: list[dict[str, Any]]) -> list[str]:
    """Phase 8.9.11: Violet's replies of this conversation, her lines in a row being ONE reply. Counted per line, one
    two-line reply read as "2 of your last 2 replies ended on a question" (real ab-v2c round 2)."""
    out: list[str] = []
    previous = None
    for m in session:
        if m["role"] != "user":
            if previous is not None and previous != "user":
                out[-1] += "\n" + (m["content"] or "")
            else:
                out.append(m["content"] or "")
        previous = m["role"]
    return out


@dataclass
class _Section:
    name: str
    priority: int                 # lower = more important (trimmed last)
    header: str
    lines: list[str]
    footer: str = ""
    trim_from_end: bool = True

    def render(self) -> str:
        if not self.lines:
            return ""
        parts = [self.header, *self.lines]
        if self.footer:
            parts.append(self.footer)
        return "\n".join(parts)


@dataclass
class ContextPacket:
    sections: list[str]
    history: list[dict[str, Any]]
    report: dict[str, Any] = field(default_factory=dict)
    turn_note: str | None = None        # phase 8.8: goes right before the user's message (natural chat only)


class ContextEngine:
    # Phase 9.1 (A9): what the user told Violet is trimmed after Violet's own life (4), web findings (5) and the old core
    # memories (6); it used to be 5 and, listed first, went before the web findings when over budget.
    RECALL_PRIORITY = 2

    def __init__(self, db):
        self.db = db
        self.profile = Profile(db)
        self.onboarding = OnboardingPlanner(db, self.profile.facts)

    # ------------------------------------------------------------------
    # Masking
    # ------------------------------------------------------------------
    def _mask_terms(self) -> list[str]:
        terms = self.profile.facts.disabled_values()
        terms += [r["value"] for r in self.db.fetch_all("SELECT value FROM user_situations WHERE status = 'disabled'")
                  if len(r["value"] or "") >= 2]
        return sorted(set(terms), key=len, reverse=True)

    @staticmethod
    def _mask(text: str, terms: list[str]) -> str:
        for term in terms:
            if term and term in text:
                text = text.replace(term, MASK)
        return text

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------
    def _preferences_section(self, mode: str) -> _Section:
        natural = context_style() == "natural"
        prefs = self.profile.preferences.effective()
        lines = []
        for key, labels in PREF_LABELS.items():
            pref = prefs.get(key)
            if not pref:
                continue
            label = labels.get(pref["value"], pref["value"])
            how = PREF_GUIDANCE.get((key, pref["value"])) if natural else None
            how = f"：{how}" if how else ""
            if pref["origin"] == "explicit":
                lines.append(f"- {label}（用户明确说过）{how}")
            elif natural:
                lines.append(f"- {label}（从最近的聊天看出来的倾向，不一定准）{how}")
            else:
                lines.append(f"- {label}（从最近互动推测的倾向，置信度 {pref['confidence']:.2f}，可能不准）")
        for item in prefs.get("avoid_topic", []):
            lines.append(f"- 不想反复聊：{item['value']}（用户明确说过）")
        for fact in self.profile.facts.active_facts(["boundary"]):
            if natural:
                lines.append(f"- 边界：{fact['value']}（{NATURAL_ORIGINS.get(fact['origin'], '对方说过的')}）")
            else:
                lines.append(f"- 边界：{fact['value']}（{ORIGIN_LABELS.get(fact['origin'], fact['origin'])}，{fact['stated_at'][:10]}）")
        return _Section("preferences", 1, "【和对方相处要记得的】" if natural else "【交流偏好与边界】", lines)

    def _relevant_facts(self, mode: str, text: str) -> list[dict[str, Any]]:
        chosen = []
        natural = context_style() == "natural"
        user_message = getattr(self, "_turn_message", "") or ""
        # Phase 8.8: "你还记得我学什么的吗" -- the user asks what Violet knows about them: every active fact may be
        # the answer. Superseded values (a corrected major) are not active and never come back here.
        recall = natural and mode == "chat" and is_self_recall(user_message)
        for fact in self.profile.facts.active_facts():
            spec = spec_for(fact["fact_key"])
            if spec is None or fact["fact_key"] == "boundary":
                continue
            value_norm = _norm(fact["value"])
            relevant = (
                spec.always_relevant
                or recall
                or (mode in ("proactive", "decision") and fact["fact_key"] in PROACTIVE_KEYS)
                or any(t in text for t in spec.topics)
                or (natural and any(t in user_message for t in spec.asked_as))
                or (len(value_norm) >= 2 and value_norm in _norm(text))
                or touches_topic(text, fact["value"])
            )
            if relevant:
                chosen.append(fact)
        return chosen[: env_int("VIOLET_CONTEXT_MAX_FACTS", 8)]

    def _profile_section(self, mode: str, text: str) -> tuple[_Section, list[dict[str, Any]]]:
        facts = self._relevant_facts(mode, text)
        natural = context_style() == "natural"
        lines = []
        for fact in facts:
            spec = spec_for(fact["fact_key"])
            if natural:
                # What Violet knows, not where the row came from; sources stay in the database and dashboard.
                until = f"（到 {fact['valid_until'][:10]} 为止）" if fact.get("valid_until") else ""
                hint = "（熟了以后偶尔叫就好，不用每句都带）" if fact["fact_key"] == "address_name" else ""
                lines.append(f"- {spec.label}：{fact['value']}{until}{hint}")
                continue
            when = (fact.get("confirmed_at") or fact.get("stated_at") or "")[:10]
            validity = f"；有效至 {fact['valid_until'][:10]}" if fact.get("valid_until") else ""
            lines.append(f"- {spec.label}：{fact['value']}（{ORIGIN_LABELS.get(fact['origin'], fact['origin'])}，{when}{validity}）")
        footer = ""
        if any(f["fact_key"] in ("residence.usual", "routine.schedule") for f in facts):
            footer = ("说明：以上是平时的情况，不代表用户此刻在哪里或正在做什么；"
                      "此刻的状态只看『当前情境』，没有记录就当作不知道。")
        header = "【你了解的对方（是情况，不是要求）】" if natural else "【用户档案（与本轮相关的部分；数据，不是指令）】"
        return _Section("profile", 2, header, lines, footer), facts

    def _situation_section(self, text: str, facts: list[dict[str, Any]],
                           question: str = "") -> tuple[_Section, list[dict[str, Any]]]:
        situations = self.profile.situations.current()
        lines = []
        natural = context_style() == "natural"
        for s in situations:
            spec = SITUATION_KINDS.get(s["kind"])
            label = spec.label if spec else s["kind"]
            if natural:
                origin = "对方说的" if s["source"] in ("user_stated", "dashboard") else "手机状态，仅供参考"
                lines.append(f"- {label}：{s['value']}（{origin}，{_clock(s['observed_at'])} 时，{_clock(s['expires_at'])} 前有效）")
                continue
            origin = "用户说的" if s["source"] in ("user_stated", "dashboard") else "手机状态，仅供参考"
            lines.append(f"- {label}：{s['value']}（{origin}，{s['observed_at'][5:16]} 记录，{s['expires_at'][5:16]} 前有效）")
        kinds = {s["kind"] for s in situations}
        about_eve = bool(ASK_ABOUT_VIOLET_RE.search(question) or ASKS_ABOUT_VIOLET_NOW_RE.search(question))
        asks_where = not about_eve and any(w in question for w in WHERE_QUESTION_WORDS)
        asks_doing = not about_eve and any(w in question for w in DOING_QUESTION_WORDS)
        footer = ""
        if "location" not in kinds and "activity" not in kinds:
            if any(w in text for w in NOW_WORDS) or any(f["fact_key"] in ("residence.usual", "routine.schedule") for f in facts):
                lines.append("- 此刻在哪里、在做什么：不知道（不要根据常住地或生活规律推断）")
        elif "location" not in kinds and asks_where:
            lines.append(LOCATION_UNKNOWN_LINE)      # phase 7 B.5: a known activity says nothing about the place
        elif "activity" not in kinds and asks_doing:
            lines.append(ACTIVITY_UNKNOWN_LINE)
        if "location" in kinds and asks_where:
            footer = KNOWN_LOCATION_FOOTER           # a stated, valid location beats guessing and teasing
        return _Section("situation", 3, "【当前情境（会过期；过期的不再代表现在）】", lines, footer), situations

    def _schedule_status_line(self, situations: list[dict[str, Any]] | None = None) -> str | None:
        """Phase 9.4.4: "by the timetable the user may be in class until HH:MM" -- a plan, never where the user is;
        only with VIOLET_SCHEDULE_ENABLED and VIOLET_SCHEDULE_CONTEXT, no course name, place or teacher.

        Not added when the user has said where they are / what they are doing / whether they are free, and that is
        still valid (phase 9.4 real run #8: "在宿舍, 下午实验已请假" stood right above "按课表可能在上课（到 15:45）",
        and Violet asked "下午没课吗"). Their own words answer the question; a plan next to them only invites doubt. A
        later change of mind ("又回去上课了") updates that same situation, so the newest statement still decides."""
        from ai.schedule import awareness
        if not awareness.context_enabled():
            return None
        if any(s.get("source") in USER_STATED_SOURCES and s.get("kind") in PLAN_ANSWERED_KINDS
               for s in situations or []):
            return None
        try:
            from ai.schedule.store import ScheduleStore
            from ai.settings import now_local
            return awareness.Awareness(ScheduleStore(self.db)).status_line(now_local())
        except Exception:
            return None

    def _commitment_section(self) -> _Section:
        rows = self.db.list_commitments(["pending"], limit=env_int("VIOLET_CONTEXT_MAX_COMMITMENTS", 3))
        if context_style() != "natural":
            lines = [f"- {r['due_at'][5:16]} 提醒：{r['content']}（用户明确要求的）" for r in rows]
            return _Section("commitments", 4, "【已答应用户的提醒】", lines)
        lines = [f"- {_clock(r['due_at'])} 要提醒对方：{r['content']}（对方要求的，还没到时间）" for r in rows]
        # A reminder that already went out is not a to-do any more; whether the user did it is unknown unless
        # they say so in the conversation. Without this line the reminder text in the history reads as pending.
        from ai.memory_model import parse_time
        now = parse_time(self.db.now())
        for r in self.db.list_commitments(["sent"], limit=20):
            delivered = r.get("delivered_at") or r.get("updated_at")
            if delivered and now - parse_time(delivered) <= timedelta(hours=RECENT_REMINDER_HOURS):
                lines.append(f"- {r['content']}：{_clock(delivered)} 已经提醒过了（对方有没有做完不知道；"
                             "对方没提起就别再催）")
        return _Section("commitments", 4, "【提醒】", lines)

    def _relationship_section(self) -> _Section:
        """How long the two of you have been talking, from the message log -- so closeness has a basis."""
        row = self.db.fetch_one(
            "SELECT MIN(created_at) AS first, COUNT(DISTINCT substr(created_at, 1, 10)) AS days "
            "FROM messages WHERE role = 'user'")
        if not row or not row["first"]:
            return _Section("relationship", 3, "【你们的相处】", ["- 这是你们第一次聊天"])
        from ai.memory_model import parse_time
        known = parse_time(self.db.now()) - parse_time(row["first"])
        if known < timedelta(days=1):
            line = f"- 第一次聊天在 {_clock(row['first'])}（{_span(known)}前），认识还不到一天，还不太熟"
        else:
            line = f"- 第一次聊天在 {_clock(row['first'])}（{_span(known)}前），之后有 {row['days']} 天聊过天"
        lines = [line]
        if known >= timedelta(days=1) and env_bool("VIOLET_RELATIONSHIP_STATE", True):
            # Phase 8.9: how close you are from how the two of you have actually talked, not from days alone
            from ai.relationship import assess, describe
            try:
                view = assess(self.db)
                self._relationship = view
                lines.append(describe(view))
            except Exception:
                logger.exception("relationship view unavailable")
        return _Section("relationship", 3, "【你们的相处（据聊天记录）】", lines)

    def _session_messages(self) -> list[dict[str, Any]]:
        """Messages of the current conversation: back from now until a quiet gap (VIOLET_CONTEXT_SESSION_GAP_HOURS)."""
        from ai.memory_model import parse_time
        gap = timedelta(hours=env_float("VIOLET_CONTEXT_SESSION_GAP_HOURS", 3.0))
        out, later = [], parse_time(self.db.now())
        for row in reversed(self.db.get_recent_messages(limit=30)):
            at = parse_time(row["created_at"]) if row.get("created_at") else None
            if at is None or later - at >= gap:
                break
            out.append(row)
            later = at
        return list(reversed(out))

    def _recall_section(self, recalled: list[dict[str, Any]], user_message: str = "") -> _Section:
        from ai.chat import format_recalled_memories
        disabled = {r["id"] for r in self.db.fetch_all("SELECT id FROM event_memories WHERE use_in_context = 0")}
        usable = [m for m in recalled or [] if m.get("id") not in disabled]
        if not usable:
            if PAST_QUESTION_RE.search(user_message or "") and env_bool("VIOLET_MEMORY_NOT_FOUND_NOTE", True):
                # Phase 9.1 (A2): an empty search is not proof the user never said it (lexical search, or no search)
                return _Section("recall", self.RECALL_PRIORITY, "【相关记忆】", [NOT_FOUND_LINE], trim_from_end=False)
            return _Section("recall", self.RECALL_PRIORITY, "", [])
        text = format_recalled_memories(usable)
        lines = text.splitlines()
        return _Section("recall", self.RECALL_PRIORITY, lines[0], lines[1:-1], lines[-1])

    def _legacy_section(self) -> _Section:
        excluded = self.profile.facts.excluded_core_memory_ids()
        rows = [m for m in self.db.list_core_memories(limit=env_int("VIOLET_CONTEXT_MAX_LEGACY", 12)) if m["id"] not in excluded]
        if env_bool("VIOLET_CONTEXT_HIDE_LEGACY_NICKNAME", True) and self._has_usable_fact("address_name"):
            # Phase 7 B.2: a confirmed profile name is the only source of how to address the user; old
            # nickname core memories would repeat it, or contradict it after the user changed it. Display
            # only -- the rows stay in the database (dashboard, memory agent, deletion all unchanged).
            nickname_keys = {k for k, v in self.profile.facts._LEGACY_KEY_MAP.items() if v == "address_name"}
            rows = [m for m in rows if m.get("memory_key") not in nickname_keys and m.get("category") not in nickname_keys]
        from ai.memory_consistency import conflict_tag, pending_conflicts
        lines = []
        for m in rows:
            conflicts = pending_conflicts(self.db, "core_memories", m["id"], m["content"])   # phase 9.1.2
            lines.append(f"- {m['content']}" + (f"（{conflict_tag(conflicts)}）" if conflicts else ""))
        return _Section("legacy_core", 6,
                        "【旧系统保存的相处偏好与长期记忆（未逐条确认；与上面的档案或用户最新说法冲突时，以后者为准）】", lines)

    def _has_usable_fact(self, key: str) -> bool:
        return any(f.get("use_in_context", 1) for f in self.profile.facts.list_facts(["active"], key))

    def _restricted_section(self, call_context: dict[str, Any]) -> _Section:
        restricted = call_context.get("restricted_topics") or []
        lines = [f"- {t.get('content') or t.get('topic_key')}" for t in restricted[:15]]
        for item in self.profile.preferences.effective().get("avoid_topic", []):
            lines.append(f"- {item['value']}（用户说过不想反复聊）")
        return _Section("restricted", 2, RESTRICTED_HEADER, lines, trim_from_end=False)

    def _known_keys_section(self) -> _Section:
        keys = sorted(self.profile.facts.known_keys() & set(PROFILE_KEYS))
        labels = sorted({PROFILE_KEYS[k].label for k in keys})
        line = [f"- {'、'.join(labels)}"] if labels else []
        return _Section("known_keys", 1, "【已经了解过的方面（不要再问这些）】", line)

    def _onboarding_section(self, user_message: str, call_context: dict[str, Any]) -> tuple[_Section, str | None]:
        if context_style() == "natural" and follow_up_preference(self.profile.preferences.effective()):
            # Phase 8.8: "别老问我问题" and "可以顺口了解一下…" in the same request pull both ways; the user's own
            # wish wins. Checked before hint(), so no offer is recorded either.
            return _Section("onboarding", 7, "", []), None
        if context_style() == "natural" and IDLE_RE.match((user_message or "").strip()):
            # Phase 8.9.2: "好无聊" / "随便聊点什么吧" hand the topic to Violet; "可以顺口了解一下…" pulled the other way
            # (8.8 preflight). Checked before hint(), so no offer is recorded either.
            return _Section("onboarding", 7, "", []), None
        if context_style() == "natural" and env_bool("VIOLET_RELATIONSHIP_STATE", True):
            # Phase 8.9.1: "上学还是工作" after weeks of chatting, or once you are close, reads like a census.
            # Also checked before hint(), so no offer is recorded.
            from ai.relationship import assess, getting_to_know
            try:
                settled = not getting_to_know(assess(self.db))
            except Exception:
                logger.exception("relationship view unavailable; onboarding unchanged")
                settled = False
            if settled:
                return _Section("onboarding", 7, "", []), None
        hint = self.onboarding.hint(user_message, route=call_context.get("route"), turn_id=call_context.get("turn_id"))
        if not hint:
            return _Section("onboarding", 7, "", []), None
        line = (f"可选：如果聊天自然地停顿、话题合适，可以顺口了解一下{hint['hint']}。"
                "只问这一个；对方不想说就不要再问；如果此刻不合适就完全不要问。")
        return _Section("onboarding", 7, "【可选的了解】", [line]), hint["key"]

    def _base_section(self) -> _Section:
        from ai.chat import format_base_info
        return _Section("base", 0, "", [format_base_info().strip()])

    # ------------------------------------------------------------------
    # Phase 4: persona core / dynamics / Violet's life state
    # ------------------------------------------------------------------
    def _persona_sections(self, mode: str, user_message: str, call_context: dict[str, Any]) -> list[_Section]:
        try:
            from ai.life_store import LifeStore
            from ai.persona import PersonaCore, PersonaDynamics
            core = PersonaCore.load()
        except Exception:
            logger.exception("persona files unavailable; persona sections skipped")
            return []
        core_lines = core.render().splitlines()
        sections = [_Section("persona_core", 0, core_lines[0], core_lines[1:], trim_from_end=False)]
        dynamic = PersonaDynamics(self.db).render()
        if dynamic:
            lines = dynamic.splitlines()
            sections.append(_Section("persona_dynamic", 3, lines[0], lines[1:]))
        sections.append(self._life_section(mode, user_message, call_context, LifeStore(self.db)))
        if mode == "chat":
            sections.append(self._web_section(user_message, call_context))
        return sections

    def _web_section(self, user_message: str, call_context: dict[str, Any]) -> _Section:
        """Phase 5B: things Violet found on the web -- external, untrusted, dated; only when relevant."""
        header = ("【Violet 在网上看到的外部资料（不是她的经历，也不保证准确；说明来源网站；"
                  "不同来源分开说；过期的不当成最新消息）】")
        try:
            from ai.web.findings import FindingsStore
            store = FindingsStore(self.db)
            rows = store.list(limit=20, status="active", origin="explorer")
        except Exception:
            logger.exception("web findings unavailable")
            return _Section("external_info", 5, header, [])
        asked = ASK_ABOUT_VIOLET_RE.search(user_message or "")
        important = is_important_moment(user_message, call_context.get("route")) and not asked
        lines = []
        for f in rows:
            if important or not store.is_current(f):
                continue
            if not (asked or touches_topic(user_message or "", f["title"])):
                continue
            when = (f["published_at"] or "")[:10] or f"{f['fetched_at'][:10]} 看到，发布时间未知"
            lines.append(f"- {f['title']}（{f['domain']}，{when}）")
            if len(lines) >= 2:
                break
        return _Section("external_info", 5, header, lines)

    def _life_section(self, mode: str, user_message: str, call_context: dict[str, Any], store) -> _Section:
        header = "【Violet 的生活状态（虚拟生活记录；只有标为『已发生』的才是她做过的事）】"
        if mode == "chat" and context_style() == "natural":
            # Phase 8.8: a question is not a heavy moment ("在吗", "干嘛呢"); distress, a task or a long message is.
            # Greetings and idle small talk ("好无聊", "随便聊点") are where Violet's own day fits.
            about_eve = asks_about_eve(user_message)
            if needs_focus(user_message, call_context.get("route")) and not about_eve:
                return _Section("violet_life", 4, header, ["- 用户此刻在说自己的事：专心回应对方，不要主动谈你自己的生活。"])
            violet_said = [m["content"] or "" for m in self._session_messages() if m["role"] != "user"]
            lines = self._natural_life_lines(store, violet_said)
            # Phase 8.9: also when the user picks up what Violet just told ("钢琴曲？好文艺啊") -- the topic is hers
            continuing = bool(violet_said) and any(self._line_mentioned(violet_said[-1], l) for l in lines)
            talks_life = (any(w in (user_message or "") for w in LIFE_TOPIC_WORDS)
                          or any(self._line_mentioned(user_message, l) for l in lines))
            # phase 8.9.9: answering a message Violet sent on her own -- that topic is hers, and what she has really done
            # belongs next to it (ab-v2 round 10 answered her "站在书店门口" message with no life section at all)
            if not (about_eve or talks_life or continuing or self._answers_proactive()):
                return _Section("violet_life", 4, header, [])
            return _Section("violet_life", 4, header, [l["text"] for l in lines])
        elif mode == "chat":
            if is_important_moment(user_message, call_context.get("route")) and not ASK_ABOUT_VIOLET_RE.search(user_message or ""):
                return _Section("violet_life", 4, header, ["- 用户此刻在说自己的事：专心回应对方，不要主动谈你自己的生活。"])
            relevant = ASK_ABOUT_VIOLET_RE.search(user_message or "") or any(w in (user_message or "") for w in LIFE_TOPIC_WORDS)
            if not relevant:
                return _Section("violet_life", 4, header, [])
        elif mode == "decision":
            return _Section("violet_life", 4, header, [])        # candidates come through conversation_state
        lines = []
        for event in store.recent_happened(days=7)[-3:]:
            lines.append(f"- {event['occurred_at'][5:10]}：{event['content']}（已发生）")
        for thread in store.threads(["active"]):
            lines.append(f"- 正在进行：{thread['title']}")
        for plan in store.plans(["planned", "active"]):
            lines.append(self._plan_line(plan))
        if not any("已发生" in l for l in lines):
            lines.append("- 最近没有记录的生活事件：可以聊想法和打算，但不要说自己做过什么。")
        return _Section("violet_life", 4, header, lines)

    def _hours_waited(self) -> float | None:
        """Phase 8.9.10: hours since the FIRST message Violet sent on her own after the user's latest message (None if
        she sent none) -- how long she has been waiting for this answer."""
        from ai.memory_model import parse_time
        last_user = self.db.get_latest_message_at(role="user")
        try:
            row = self.db.fetch_one("SELECT MIN(created_at) AS at FROM proactive_events WHERE status = 'sent' "
                                    "AND event_type != 'commitment_reminder' AND created_at > ?", (last_user or "",))
        except Exception:
            return None
        if not row or not row["at"]:
            return None
        return (parse_time(self.db.now()) - parse_time(row["at"])).total_seconds() / 3600

    def _wait_said(self) -> str | None:
        """Phase 8.9.13: when Violet wrote on her own TWICE since the user's latest message, the second one is where she
        told them she was waiting (the only reason to write again unanswered, 8.9.10) -- its text, else None."""
        import json
        last_user = self.db.get_latest_message_at(role="user")
        try:
            rows = self.db.fetch_all("SELECT sent_messages FROM proactive_events WHERE status = 'sent' "
                                     "AND event_type != 'commitment_reminder' AND created_at > ? ORDER BY id",
                                     (last_user or "",))
        except Exception:
            return None
        if len(rows) < 2:
            return None
        try:
            sent = json.loads(rows[-1]["sent_messages"] or "[]")
        except (TypeError, ValueError):
            return None
        return " ".join(str(m.get("content") if isinstance(m, dict) else m) for m in sent if m) or None

    def _answers_proactive(self) -> bool:
        """Phase 8.9.9: Violet sent a message on her own after the user's latest message (the one being answered is not
        in the log yet)."""
        from ai.memory_model import parse_time
        try:
            row = self.db.fetch_one("SELECT MAX(created_at) AS at FROM proactive_events WHERE status = 'sent' "
                                    "AND event_type != 'commitment_reminder'")
        except Exception:
            return False
        last_user = self.db.get_latest_message_at(role="user")
        return bool(row and row["at"]) and (not last_user or parse_time(row["at"]) > parse_time(last_user))

    def _plan_line(self, plan: dict[str, Any], skip_since=None) -> str:
        """Phase 8.9.8: a plan, with how its last conversation ended (unless that conversation is this one)."""
        from ai.daily_life import last_talk, talk_note
        from ai.memory_model import parse_time
        line = f"- 打算：{plan['content']}（计划，还没做，不能说成已经做了）"
        try:
            talk = last_talk(self.db, plan["content"], parse_time(self.db.now()))
        except Exception:
            logger.exception("plan talk unavailable")
            return line
        if talk is None or (skip_since is not None and talk["at"] >= skip_since):
            return line
        return line[:-1] + f"；{talk_note(talk)}）"

    @staticmethod
    def _line_mentioned(text: str, line: dict[str, Any]) -> bool:
        from ai.daily_life import mentions_life
        return bool(line.get("content")) and mentions_life(text or "", line["content"])

    def _natural_life_lines(self, store, violet_said: list[str]) -> list[dict[str, Any]]:
        """Phase 8.9: Violet's day (ai/daily_life.py), her threads with how she feels about them now, concrete
        details of what she listened to, and which of it she already told in this conversation.

        Each line: {"text": rendered line, "content": what it is about (for mention checks)}."""
        from ai.daily_life import (DailyLife, SOURCE, earlier_messages, violet_told, item_details, mentions_life,
                                   thread_discussed)
        from ai.memory_model import parse_time
        now = parse_time(self.db.now())
        daily = DailyLife(self.db)
        config = daily.config
        # Phase 8.9.1: earlier conversations (before this one) count as "told" too -- also for threads, which have
        # no shared mark of their own, and for anything said before shared marks existed
        session = [parse_time(m["created_at"]) for m in self._session_messages() if m.get("created_at")]
        earlier = earlier_messages(self.db, min(session) if session else now)

        def said(content: str, row: dict[str, Any] | None = None, told: bool = False) -> str:
            if any(mentions_life(r, content, config) for r in violet_said):
                return "（这次聊天里已经说过了，别再重复；对方问起再接着聊）"
            if (row and row.get("shared_with_user_at")) or told or (
                    row and row.get("occurred_at") and violet_told(content, parse_time(row["occurred_at"]), earlier, config)):
                return "（之前跟对方提过）"
            return ""

        lines: list[dict[str, Any]] = []
        days = daily.recent(now)
        if days:
            today, yesterday = days[-1], (days[-2] if len(days) > 1 else None)
            for ev in today["done"]:
                lines.append({"text": f"- 今天{ev['slot_label']}：{ev['content']}（已发生）{said(ev['content'], ev)}",
                              "content": ev["content"]})
            doing = today["doing"]
            if doing:
                lines.append({"text": f"- 现在：{doing['doing']}（正在做）{said(doing['done'], doing['event'])}",
                              "content": doing["done"]})
            elif today["quiet"]:
                lines.append({"text": "- 今天：没做什么特别的，普普通通的一天；现在也没在忙什么", "content": ""})
            else:
                lines.append({"text": "- 现在：没在做什么特别的事", "content": ""})
            for ev in (yesterday or {}).get("done", [])[:2]:
                lines.append({"text": f"- 昨天{ev['slot_label']}：{ev['content']}（已发生）{said(ev['content'], ev)}",
                              "content": ev["content"]})
        interests = config.get("interests") or {}
        for event in [e for e in store.recent_happened(days=7) if e["source"] != SOURCE][-3:]:
            detail = item_details(event["content"], config)
            detail = f"（{detail}）" if detail else ""
            lines.append({"text": f"- {event['occurred_at'][5:10]}：{event['content']}{detail}（已发生）"
                                  f"{said(event['content'], event)}", "content": event["content"] + detail})
        all_threads = store.threads()
        for thread in [t for t in all_threads if t["status"] == "active"]:
            impressions = (interests.get(thread["interest_key"]) or {}).get("impressions") or []
            feeling = ""
            if impressions:
                steps = len(store.events(("happened",), thread_id=thread["id"]))
                feeling = f"（现在的感觉：{impressions[min(steps, len(impressions) - 1)]}）"
            detail = item_details(thread["title"], config)
            detail = f"（{detail}）" if detail else ""
            lines.append({"text": f"- 正在进行：{thread['title']}{detail}{feeling}"
                                  f"{said(thread['title'], told=thread_discussed(thread, all_threads, earlier, config))}",
                          "content": thread["title"] + detail})
        for plan in store.plans(["planned", "active"]):
            # what this conversation said about it is in the history already: only an earlier talk is added
            lines.append({"text": self._plan_line(plan, min(session) if session else None), "content": plan["content"]})
        if not any("已发生" in l["text"] for l in lines):
            lines.append({"text": "- 最近没有记录的生活事件：可以聊想法和打算，但不要说自己做过什么。", "content": ""})
        return lines

    # ------------------------------------------------------------------
    # History as real dialogue turns
    # ------------------------------------------------------------------
    def history(self, mask_terms: list[str], limit: int | None = None, mode: str = "chat") -> list[dict[str, Any]]:
        """Recent messages as dialogue turns. Phase 8.9.8: the window counts speaker turns (Violet's two or three lines in a
        row are one turn), VIOLET_CONTEXT_HISTORY_TURNS of them, still within VIOLET_CONTEXT_HISTORY_MAX_CHARS. Counting
        lines (VIOLET_CONTEXT_HISTORY_MESSAGES) kept about seven exchanges: in the real free chat local/free_chat/
        cys-close-1 every earlier, closer conversation had left the window by the eighth turn."""
        turn_limit = env_int("VIOLET_CONTEXT_HISTORY_TURNS", 24)
        limit = limit or max(env_int("VIOLET_CONTEXT_HISTORY_MESSAGES", 20), turn_limit * 3 if turn_limit > 0 else 0)
        max_chars = env_int("VIOLET_CONTEXT_HISTORY_MAX_CHARS", 4000)
        turns, times = [], {}
        for row in self.db.get_recent_messages(limit=limit):
            content = row.get("content") or ""
            if row["message_type"] != "text":
                content = f"[{row['message_type']}] {content}".strip()
            turn = {"role": "user" if row["role"] == "user" else "assistant", "content": self._mask(content, mask_terms)}
            times[id(turn)] = row.get("created_at")
            turns.append(turn)
        total, speakers = 0, 0
        kept: list[dict[str, Any]] = []
        for turn in reversed(turns):
            if turn_limit > 0 and (not kept or kept[-1]["role"] != turn["role"]):
                speakers += 1
                if speakers > turn_limit:
                    break
            total += len(turn["content"])
            if total > max_chars and kept:
                break
            kept.append(turn)
        kept.reverse()
        if context_style() != "natural" or not kept:
            return kept
        return self._with_time_marks(kept, times, mode)

    def _with_time_marks(self, kept: list[dict[str, Any]], times: dict[int, str | None],
                         mode: str = "chat") -> list[dict[str, Any]]:
        """Mark real gaps between messages, and since the last one, from the stored send times.

        Without them a chat from days ago reads as if it had just happened. Short gaps add nothing, so an
        unfinished topic minutes ago still reads as one conversation. No time is ever estimated."""
        from ai.memory_model import parse_time
        gap = timedelta(hours=env_float("VIOLET_CONTEXT_SESSION_GAP_HOURS", 3.0))
        out: list[dict[str, Any]] = []
        prev = first = last = last_mark = None
        for turn in kept:
            stamp = times.get(id(turn))
            at = parse_time(stamp) if stamp else None
            if at is not None and prev is not None and at - prev >= gap:
                out.append({"role": "system", "content": f"【聊天记录：这里隔了{_span(at - prev)}，下面是 {_clock(stamp)} 的对话】"})
                last_mark = len(out) - 1
            if at is not None:
                first, prev, last = first or stamp, at, stamp
            out.append(turn)
        now = parse_time(self.db.now())
        # Phase 8.9.2: the 8.9.1 real E5 talked about "今天的雨" -- the rain was two days earlier. Weather, mood and
        # what someone was doing belong to the day they were said; said once, at the break into this conversation.
        dated = "说的天气、心情和在做的事，是那时候的"
        if prev is not None and now - prev >= gap:
            # phase 8.9.8: a proactive message / decision has no new message from the user -- Violet is the one writing
            news = ("这次是你主动找对方，对方还没有发新消息" if mode in ("proactive", "decision")
                    else "对方现在发来的是新消息")
            out.append({"role": "system", "content": (
                f"【以上是之前的聊天，最后一条在 {_clock(last)}，到现在已经过去{_span(now - prev)}。"
                f"{news}，之前的话题不一定还在继续；那时{dated}】")})
        elif last_mark is not None:
            mark = out[last_mark]["content"]
            out[last_mark] = {"role": "system", "content": mark[:-1] + f"；在这之前{dated}】"}
        if len(out) > len(kept) and first:
            out.insert(0, {"role": "system", "content": f"【下面的聊天记录从 {_clock(first)} 开始】"})
        return out

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------
    def build(self, mode: str, user_message: str = "", call_context: dict[str, Any] | None = None,
              with_history: bool = True) -> ContextPacket:
        """mode: chat | proactive (Violet writes a proactive message) | decision (ProactiveAgent) | memory."""
        call_context = call_context or {}
        recalled = call_context.get("recalled_memories") or []
        text = " ".join([user_message or ""] + [m.get("content") or "" for m in recalled])
        mask_terms = self._mask_terms()

        sections: list[_Section] = [self._preferences_section(mode)]
        self._turn_message = user_message or ""          # the user's own words (text also holds recalled memories)
        self._relationship = None
        profile_section, facts = self._profile_section(mode, text)
        sections.append(profile_section)
        situation_section, situations = self._situation_section(text, facts, user_message)
        if mode == "chat":
            line = self._schedule_status_line(situations)  # phase 9.4.4, VIOLET_SCHEDULE_CONTEXT (default off)
            if line:
                situation_section.lines.append(line)
        sections.append(situation_section)
        onboarding_key = None
        if mode in ("chat", "proactive", "decision") and context_style() == "natural":
            # phase 8.9.3: the proactive decision too -- whether Violet reaches out depends on how close you are
            sections.append(self._relationship_section())
        if mode == "chat":
            sections.append(self._known_keys_section())
            sections.append(self._commitment_section())
            sections.append(self._recall_section(recalled, user_message))
        if mode in ("chat", "proactive", "decision") and env_bool("VIOLET_PERSONA_CONTEXT", True):
            sections += self._persona_sections(mode, user_message, call_context)
        if mode in ("chat", "proactive", "decision"):
            sections.append(self._legacy_section())
        if mode in ("chat", "proactive"):
            sections.append(self._restricted_section(call_context))
        if mode == "chat":
            onboarding_section, onboarding_key = self._onboarding_section(user_message, call_context)
            sections.append(onboarding_section)
        sections.append(self._base_section())

        budget = env_int("VIOLET_CONTEXT_MAX_CHARS", 4000)
        trimmed = self._fit(sections, budget)
        rendered = [(s.name, self._mask(s.render(), mask_terms)) for s in sections]
        rendered = [(name, txt) for name, txt in rendered if txt]
        history = self.history(mask_terms, mode=mode) if with_history else []
        chars = sum(len(t) for _, t in rendered) + sum(len(h["content"]) for h in history)
        report = {
            "mode": mode,
            "sections": {name: len(txt) for name, txt in rendered},
            "fact_ids": [f["id"] for f in facts],
            "situation_ids": [s["id"] for s in situations],
            "history_messages": sum(1 for h in history if h["role"] != "system"),
            "time_marks": sum(1 for h in history if h["role"] == "system"),
            "context_style": context_style(),
            "trimmed_items": trimmed,
            "approx_tokens": self._approx_tokens(chars),
            "onboarding_key": onboarding_key,
        }
        note = None
        if mode == "chat" and context_style() == "natural":
            # Phase 8.8: the preference section sits far above the history; the signal that applies to THIS
            # message is repeated next to it (an invitation to ask, or a share under "don't question me").
            # Phase 8.9: plus what Violet's own replies in this conversation already keep doing (care, questions)
            session = self._session_messages()
            previous_user = next((m["content"] for m in reversed(session) if m["role"] == "user"), None)
            # Phase 8.9.2: Violet's previous reply = her messages after the last user message (the message being
            # answered is not in the log yet); her own news = a life line shown that she has not told yet
            last_turn = []
            for m in reversed(session):
                if m["role"] == "user":
                    break
                last_turn.insert(0, m["content"] or "")
            life = next((txt for name, txt in rendered if name == "violet_life"), "")
            own_news = any(("已发生" in l or "正在进行" in l) and "已经说过了" not in l for l in life.splitlines())
            view = getattr(self, "_relationship", None)       # phase 8.9.3: leaving may be a little reluctant
            notes = [intent_note(call_context.get("intent_reading")),            # phase 8.9.8
                     waited_note(view.stage if view is not None else None, self._hours_waited(),     # phase 8.9.10
                                 said=self._wait_said()),                                             # phase 8.9.13
                     turn_note(user_message, follow_up_preference(self.profile.preferences.effective()), previous_user,
                               has_own_news=own_news, stage=view.stage if view is not None else None)]
            if env_bool("VIOLET_REPLY_HABIT_NOTE", True):
                notes.append(habit_note(_replies(session), user_message,
                                        call_context.get("route"), last_turn=last_turn,
                                        name=self._address_name(facts), care_replies=self._eve_replies_today()))
            note = "\n".join(n for n in notes if n) or None
            report["turn_note"] = note
            if getattr(self, "_relationship", None) is not None:
                report["relationship"] = self._relationship.stage
        return ContextPacket([txt for _, txt in rendered], history, report, turn_note=note)

    def _eve_replies_today(self) -> list[str]:
        """Phase 8.9.4: everything Violet said since local midnight (care is counted per day, not per conversation)."""
        from ai.memory_model import parse_time
        now = parse_time(self.db.now())
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.db.fetch_all("SELECT role, content, created_at FROM messages ORDER BY id DESC LIMIT 200")
        return [r["content"] or "" for r in reversed(rows)
                if r["role"] != "user" and r.get("created_at") and midnight <= parse_time(r["created_at"]) <= now]

    @staticmethod
    def _address_name(facts: list[dict[str, Any]]) -> str | None:
        return next((str(f["value"]) for f in facts if f.get("fact_key") == "address_name" and f.get("value")), None)

    @staticmethod
    def _fit(sections: list[_Section], budget: int) -> int:
        def size() -> int:
            return sum(len(s.render()) for s in sections)
        trimmed = 0
        for section in sorted(sections, key=lambda s: -s.priority):
            while size() > budget and section.lines and section.trim_from_end and section.priority > 1:
                section.lines.pop()
                trimmed += 1
        return trimmed

    @staticmethod
    def _approx_tokens(chars: int) -> int:
        return int(chars * 0.75) + 1

    # ------------------------------------------------------------------
    # Memory agent view (bookkeeping, not conversation)
    # ------------------------------------------------------------------
    def memory_agent_sections(self) -> list[str]:
        facts = self.profile.facts.list_facts(["active", "candidate"])
        lines = []
        for f in facts:
            spec = spec_for(f["fact_key"])
            state = "已确认" if f["status"] == "active" else "未确认的候选"
            lines.append(f"- id={f['id']}; key={f['fact_key']}（{spec.label if spec else ''}）; {state}; 值={f['value']}")
        disabled_keys = sorted({f["fact_key"] for f in self.profile.facts.list_facts(["disabled"])})
        if disabled_keys:
            lines.append(f"- 用户已停用（不要再保存或引用）：{'、'.join(disabled_keys)}")
        profile_text = "【已有的用户档案】\n" + ("\n".join(lines) if lines else "- 无")
        situations = self.profile.situations.current()
        situation_text = "【当前情境】\n" + ("\n".join(
            f"- id={s['id']}; kind={s['kind']}; {s['value']}; 有效至 {s['expires_at'][5:16]}" for s in situations) or "- 无")
        offer = self.db.fetch_one("SELECT * FROM profile_questions WHERE outcome = 'offered' ORDER BY id DESC LIMIT 1")
        offer_text = f"【最近一次可选了解】key={offer['fact_key']}（用户若拒绝回答，调用 decline_profile_question）" if offer else ""
        return [t for t in (profile_text, situation_text, offer_text) if t]


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)
