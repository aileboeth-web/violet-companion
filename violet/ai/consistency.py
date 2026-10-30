"""Consistency guard (phase 4): checks run in code, not by prompt.

1. check_event(proposal)  -- before a life event is stored:
     external_as_experience   internet/news material (or a public fact) proposed as something Violet lived
     reading_record_*         a reading record must only say "read an article about X" and carry its url
     future_event             occurs after "now"
     thread_not_active        thread missing or not active
     before_thread_start      earlier than the thread began
     out_of_order             earlier than the thread's latest event
     time_conflict            overlaps another recorded event
     plan_not_open            completes a plan that is not open / not in this thread
     completion_without_plan  says "finished X" without resolving the plan
     forbidden_backstory      family / job / school / romance ... (persona core)
     identity_conflict        claims to be human etc. (persona core)
     user_experience          looks like (or is marked as) the USER's experience
     too_dramatic             accident, illness, moving abroad ...
     duplicate                same content in this thread recently
   Any violation -> nothing is written; a consistency_flag is recorded.

2. check_reply(text)  -- Violet's generated message, line by line:
     unrecorded_experience    "我昨天去了…" with no recorded happened event behind it
     plan_described_as_done   talks about a plan (not completed) as already done
     user_experience_as_own   retells the user's experience as Violet's
     forbidden_backstory / identity_conflict
     external_as_experience   retells a web finding as something Violet did (phase 5B)
     camera_claim             presents a picture as a photo / selfie / camera shot (phase 6)
   certain:   forbidden_backstory, identity_conflict, plan_described_as_done, external_as_experience, camera_claim
   uncertain: unrecorded_experience, user_experience_as_own -- may be reviewed by
              the optional semantic check (ai/semantic_guard.py)
   The caller (ai.chat.guard_reply) asks for one natural rephrase, then drops
   only the lines that still fail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ai.memory_model import mentions_topic, parse_time, touches_topic
from ai.persona import PersonaCore, life_config
from ai.settings import env_str

EXTERNAL_ORIGINS = {"web", "news", "search", "external", "rss", "web_mcp"}
COMPLETION_RE = re.compile(r"(读完|听完|写完|看完|整理好|整理完|整理成|完成了|听熟了)")
DRAMATIC_RE = re.compile(r"(车祸|事故|住院|手术|生病|分手|失业|辞职|中奖|出国|移民|搬家|去世|死了|吵架|打架|被偷|受伤)")
# first person + a completed action within the same clause: "我昨天把《边城》读完了", "昨天我去了…"
_PAST_VERBS = (r"(?:去了|读了|看了|听了|写了|买了|学了|练了|吃了|玩了|做了|逛了|见了|弹了|画了|记了|整理了|"
               r"爬了|跑了|游了|骑了|拍了|唱了|喝了|参加了|报了|逛完|爬完|跑完|"
               r"读完|看完|听完|写完|整理好|整理完|听熟了)")
PAST_CLAIM_RE = re.compile(r"我[^，,。！!？?；;、\n]{0,12}?" + _PAST_VERBS)
# Phase 8.9.10 (real free chat ab-v2b): Chinese drops the "我" -- "看了开头几章，还没到特别揪心的地方" (asked "好看吗",
# about a book she had only planned to pick up). A clause that starts with the completed action is her claim too.
SUBJECTLESS_CLAIM_RE = re.compile(r"^(?:《[^》]{1,20}》|这本书?|那本书?)?(?:我)?"
                                  r"(?:刚|刚刚|已经|今天|昨天|前天|早上|上午|中午|下午|晚上|这两天|最近)?(?:又|也|才)?"
                                  + _PAST_VERBS)
# phase 8.9.13 (real ab-v2e): fear, worry, guesses and hearsay before the action are no claim either --
# "我怕我看完难受好几天", "都说看完心里沉甸甸的"
_HEDGE = (r"(没|不|别|想|打算|准备|要不要|要是|如果|会不会|可能会|下次|以后|觉得|感觉|好像|"
          r"怕|担心|万一|估计|可能|也许|会|听说|都说|据说|有人说|别人说|听人说)")
# "这名字我听过好多回了": heard OF a name / a title / what people say -- knowledge, not having listened to something
# words that only make sense after something said before them
_LEANING_START_RE = re.compile(r"^(不过|但是|可是|只是|而且|所以|然后|另外|再说)[，,、\s]*")
_HEARSAY_RE = re.compile(r"(名字|这名|书名|片名|听说|有人说|都说|据说|人提|提起|说起|听人)")
_VERB_FORMS = (r"(看了一下|看了看|想了想|看了你|听了你|吃了一惊|"
               # a reaction, not an experience: "我听了好难过", "看了都想哭"
               r"(?:听|看)了[^，,。！!？?]{0,3}(?:难过|开心|心疼|感动|生气|高兴|难受|激动|担心|羡慕|好笑|想哭|想笑))")
NOT_A_CLAIM_RE = re.compile(_HEDGE[:-1] + "|" + _VERB_FORMS[1:])
_HEDGE_RE, _VERB_FORMS_RE = re.compile(_HEDGE), re.compile(_VERB_FORMS)
_QUESTION_END_RE = re.compile(r"(吗|么|[?？])\s*$")
# Phase 7 B.9.1: the experiential aspect is a claim of having done it too ("我也爬过山", "我去过泰山",
# "以前我爬过一次黄山", "我也去过"). Same verbs as the completed forms above -- the same policy, no wider net --
# plus going / visiting / staying somewhere. "过来 / 过去" (a direction) is not an experience.
_EXPERIENCE_VERBS = "去|读|看|听|写|买|学|练|吃|玩|做|逛|见|弹|画|记|爬|跑|游|骑|拍|唱|喝|参加|报|到|登|住"
EXPERIENCE_CLAIM_RE = re.compile(rf"我[^，,。！!？?；;、\n]{{0,12}}?(?:{_EXPERIENCE_VERBS})过(?![来去])")
# only for the experiential form: never ("从未去过") and questions ("我们一起去过吗") are not claims
EXPERIENCE_NOT_A_CLAIM_RE = re.compile(r"(从未|未曾|(?:吗|么)$)")
# a recorded "读了几章《边城》" backs "我读过《边城》": compare the experiential form as the completed one
EXPERIENCE_AS_DONE_RE = re.compile(rf"({_EXPERIENCE_VERBS})过(?![来去])")
USER_REFERENCE_RE = re.compile(r"你")
# Phase 8.9.9 (real free chat ab-v2): being somewhere or having got something, said without the completed verbs above --
# "我出门啦，人已经站在书店门口了" (the brief said "等下就要出门"), "听你的，我带回家了" (the user had only suggested it).
# The same records decide as for "我去了…". Suppositions and plans ("等我到了", "待会儿出门", "周末要去"), questions and
# what the user does ("你到家了吗") are not claims.
_PLACES = r"(书店|店里|店门口|门口|公园|咖啡馆|超市|图书馆|商场|书架前|柜台)"
STATE_CLAIM_RE = re.compile(rf"((出门|出发)(了|啦)|已经(站在|在|到)[^，,。！!？?]{{0,8}}{_PLACES}|"
                            rf"(站在|坐在|到了|到啦)[^，,。！!？?]{{0,6}}{_PLACES}|到{_PLACES}(了|啦)|"
                            r"(带|拿|买|借)回(家|来)(了|啦)|(买|拿|借)到(了|啦)|(回到家|到家)(了|啦))")
# "站在书架前肯定又要纠结半天" (the real 22:01 message) is what happens whenever she is there, not where she is now
STATE_HEDGE_RE = re.compile(r"(等|待会|一会儿|等会|等下|马上|要|会|肯定|总|每次|时候|的话|才|要是|如果|打算|准备|想|"
                            r"下次|以后|明天|周末|吗|么|？|\?)")
# Phase 9.3: the act of looking something up online this turn ("刚查了一下", "看了原文", "搜到一篇") -- not a lived
# experience ("去了", "吃了", "见到" stay checked: a page's content is never Violet's life).
WEB_ACTION_RE = re.compile(
    r"(查|搜|搜索|看|读|翻|点开)(了|过|到)?(一下|下|一遍)?(网上|网页|原文|文章|这篇|那篇|报道|新闻|资料|搜索结果|链接|结果|官网|公告)"
    r"|(刚|刚刚|刚才)(帮你)?(查|搜|搜索|看)(了|过)?(一下|下)?"
    r"|(网上|搜索结果|原文|那篇|这篇)(里|上)?(说|写|提到)")
LIVED_RE = re.compile(r"(去了|去过|吃了|喝了|见了|见到|买了|玩了|参加了|逛了|拍了)")
# Phase 9.4.2: the act of looking at the user's own timetable this turn ("看了下你的课表", "查了一下课表").
SCHEDULE_ACTION_RE = re.compile(
    r"(查|看|翻|对)(了|过)?(一下|下|一眼|了一下)?(你的)?(课表|课程表)"
    r"|(刚|刚刚|刚才)(帮你)?(查|看|翻)(了|过)?(一下|下)?")

# Phase 6: Violet's pictures are AI-generated illustrations, never photos (first person only:
# "你的自拍好可爱" is fine).
CAMERA_CLAIM_RE = re.compile(
    r"(我的?自拍|我(?:刚|刚刚|刚才|今天|昨天)?(?:用手机)?(?:拍|照)(?:的|了|下|给你)|(?:用|我的)摄像头|实拍|"
    r"这是(?:一张)?真实的?照片|真人照片|现拍|原图直出|手机拍的)")
CAMERA_NEGATED_RE = re.compile(r"(不是|并非|不算|没有|没|不能算)[^，,。！!？?]{0,6}(自拍|拍|照片|摄像头|实拍)")
CLAUSE_SPLIT_RE = re.compile(r"[，,。！!？?；;~～…]+")
# Phase 9.4 (real run cys-p94-sched-1 #4, "我们英语老师要是也这样就好了"): the persona's school / work patterns only see
# "我们老师", "我们的同学"; a word between the possessive and the role ("我们英语老师", "我的数学同学", "我们这边公司")
# slipped through. Up to four characters are allowed there -- but not an action or a link ("我们一起去问老师",
# "如果我们是同学" stay allowed: they do not say Violet has that teacher / class / company). Hypotheticals without the
# possessive ("要是我也有个英语老师") are not touched.
_OWNED_ROLE_RE = re.compile(r"(我们|我的)的?([一-鿿]{0,4}?)(老师|导师|同学|室友|班主任|辅导员|学校|班上|公司|单位|老板|同事|领导)")
_NOT_A_MODIFIER = set("去问找跟和给让叫说告帮请见想要会能起也都还就是像当被把对向天")    # 天: "我的天" (an exclamation)


def owned_role_claim(text: str) -> str | None:
    """"我们英语老师" -> the matched words; None when nothing claims a teacher / class / company of Violet's own."""
    for match in _OWNED_ROLE_RE.finditer(text or ""):
        if not set(match.group(2)) & _NOT_A_MODIFIER:
            return match.group(0)
    return None


# Phase 8.9.4: bigrams that only carry grammar. "我刚刚也随手听了会儿歌" was "backed" by the recorded "在公园散了会儿步"
# through 了会 / 会儿 alone (8.9.2 real E1). Backing a claim needs shared CONTENT.
FILLER_BIGRAMS = frozenset({"了会", "会儿", "一会", "了一", "一下", "一点", "有点", "一遍", "了个", "一个", "一些", "了些",
                            "了下", "下了", "着呢", "了吧", "了呀", "的时", "时候", "一直", "然后", "就是", "还是", "了好"})


def _content_bigrams(text: str) -> set[str]:
    from ai.memory_model import _bigrams
    return _bigrams(text) - FILLER_BIGRAMS


def backed_by(text: str, content: str) -> bool:
    """A recorded event backs a line when they share content -- same thresholds as mentions_topic, without fillers."""
    topic = _content_bigrams(content)
    if not topic:
        return False
    hits = topic & _content_bigrams(text)
    if len(topic) <= 4:
        return len(hits) >= 1
    return len(hits) >= 2 and len(hits) / len(topic) >= 0.25


_POSSESSION_CLAIM_RE = re.compile(r"(带|拿|买|借)(回|到)")
_POSSESSION_EVENT_RE = re.compile(r"(买|挑|借|拿|带回|选了|入手)")


def _state_backed(line: str, recent: list[dict], now) -> bool:
    """Phase 8.9.9: "人已经站在书店门口了" is backed by a recorded trip to the bookstore within the last day, "带回家了" by
    something recorded as bought / picked / borrowed then -- being somewhere or having something follows from them."""
    places = set(m.group(0) for m in re.finditer(_PLACES, line))
    possession = bool(_POSSESSION_CLAIM_RE.search(line))
    for event in recent:
        if now - parse_time(event["occurred_at"]) > timedelta(hours=24):
            continue
        content = event["content"] or ""
        if any(p in content for p in places) or (possession and _POSSESSION_EVENT_RE.search(content)):
            return True
    return False


# what an earlier clause of the same sentence sets in the future or the conditional: "我大概会挑个白天看，看完了去公园
# 走走" (real ab-v2) -- the subjectless "看完了" is part of that plan
_FUTURE_RE = re.compile(r"(会|要|打算|想|准备|等|如果|要是|的话|周末|明天|下次|以后|待会|一会儿|到时候)")


# a subjectless clause that asks ("看了什么电影呀？") is a question to the user, not her claim
# ("几" / "多少" are left out: "看了开头几章" is a claim)
_ASKING_WORD_RE = re.compile(r"(什么|啥|哪|谁|怎么|有没有|是不是)")


def _claims(line: str) -> list[str]:
    out, future = [], False
    pieces = re.split(f"({CLAUSE_SPLIT_RE.pattern})", line)
    for i in range(0, len(pieces), 2):
        clause, after = pieces[i], (pieces[i + 1] if i + 1 < len(pieces) else "")
        asking = bool(re.search(r"[?？]", after) or _ASKING_WORD_RE.search(clause))
        part = _claim(clause, subjectless=not (future or asking))
        if part is not None:
            out.append(part)
        future = future or bool(_FUTURE_RE.search(clause))
    return out


def _claim(clause: str, subjectless: bool = True) -> str | None:
    """The part of a clause that claims Violet did something (from its start to the claimed action), or None.

    Phase 8.9.10: for completed actions a hedge or "你" counts only BEFORE the action -- "我记了两处想跟你说的" is a claim
    (想 / 你 are about the notes), "我想去了" / "我陪你去了" are not her claim. Verb forms that are no experience
    ("看了一下", "看了你发的", "听了好难过") still leave the whole clause out."""
    clause = clause.strip()
    if not clause or _VERB_FORMS_RE.search(clause):
        return None
    m = PAST_CLAIM_RE.search(clause) or (SUBJECTLESS_CLAIM_RE.search(clause)
                                         if subjectless and not _QUESTION_END_RE.search(clause) else None)
    if m is not None:
        part = clause[:m.end()]
        return None if _HEDGE_RE.search(part) else part
    if NOT_A_CLAIM_RE.search(clause):
        return None
    if EXPERIENCE_CLAIM_RE.search(clause) and not EXPERIENCE_NOT_A_CLAIM_RE.search(clause):
        if "听过" in clause and _HEARSAY_RE.search(clause):
            return None
        return clause
    if STATE_CLAIM_RE.search(clause) and not STATE_HEDGE_RE.search(clause) and not USER_REFERENCE_RE.search(clause):
        return clause
    return None


def mentions_life_strict(text: str, content: str, config: dict | None = None) -> bool:
    """Titles / configured items / their details (ai.daily_life.life_terms), or content overlap without fillers."""
    from ai.daily_life import life_terms
    return any(t in (text or "") for t in life_terms(content, config)) or backed_by(text or "", content or "")


# Phase 8.9.4: Violet has no weather of her own. "今天也冷" (8.9.2 E1, the cold was the day before), "雨天确实容易无聊" /
# "很适合这种下雨天" (8.9.2 E5, the rain was two days before) retell an EARLIER conversation's weather as now. When the
# user has not brought the weather up in this conversation, such a line is stale. "适合下雨天看" is not about now.
STALE_WEATHER_RE = re.compile(
    r"((今天|今晚|现在|外面|这会儿|此刻)(也|又|还|真|好|挺|有点|特别|这么|那么)?(冷|热|凉|闷|下雨|在下雨|下着雨|下雪|刮风|降温|晴)"
    r"|今天的雨|(这种|这样的|今天这种)(下雨天|雨天|天气)|(雨天|下雨天)(确实|真的|就是)|雨下得)")
WEATHER_WORDS_RE = re.compile(r"(冷|热|凉|雨|雪|风|降温|升温|天气|晴|阴天|闷|晒|太阳|雾|霾)")


# Phase 8.9.5: the model's own drafting notes leaking into a reply (8.9.4 real A4, model output, unchanged by Violet's
# processing): "…说起呀陪伴? no. Question okay but perhaps don't mechanical. "我给你留着位置呢" maybe service-y but
# cute. Use 2 lines." A run of 5+ English words with 3+ distinct drafting words; "I miss you so much", "好的ok",
# "赶deadline" are untouched.
_ENGLISH_RUN_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*(?:[\s,.;:!?\"“”'()/]+[A-Za-z][A-Za-z'’\-]*){4,}")
# conversational words ("okay", "I'll", "keep") are left out: "I'll be there, don't worry about it okay" is a reply
_DRAFT_WORDS = frozenset({"maybe", "perhaps", "don't", "dont", "should", "shouldn't", "use", "line", "lines",
                          "question", "questions", "reply", "replies", "tone", "mechanical", "service", "service-y",
                          "draft", "output", "user", "avoid", "natural", "sounds", "emoji", "instead",
                          "options", "option", "version"})


def leaked_draft(line: str) -> bool:
    for run in _ENGLISH_RUN_RE.finditer(line or ""):
        words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'’\-]*", run.group(0))}
        if len(words & _DRAFT_WORDS) >= 3:
            return True
    return False


@dataclass
class Violation:
    check: str
    detail: str
    line: str | None = None
    semantic: str | None = None      # phase 5A: verdict / reason from the optional semantic review


def guard_mode() -> str:
    return env_str("VIOLET_PERSONA_REPLY_GUARD", "enforce").lower()     # enforce | flag | off


def _owner_call() -> str:
    """What she calls the owner in the relationship of this channel ("" when nothing set)."""
    try:
        from ai.channels.context import current
        from ai.persona_templates import relationship
        return relationship(current().channel).get("call") or ""
    except Exception:                                     # noqa: BLE001
        return ""


class ConsistencyGuard:
    def __init__(self, db, core: PersonaCore | None = None, config: dict[str, Any] | None = None):
        self.db = db
        self.core = core or PersonaCore.load()
        self.config = config or life_config()
        # phase 8.9.4: replies to the user are checked for weather stated as now; a proactive weather task has
        # real data behind it (Performer), so guard_reply turns this on for entity="reply" only
        self.check_weather = False

    def _stale_weather(self, now: str) -> bool:
        """Weather came up in an EARLIER conversation and not in this one (the last few hours of the user's own
        messages): a weather line now can only be the old weather retold as current. With no weather in the
        history at all, a passing "今天降温了" is left alone (phase 5A: harmless chat)."""
        from datetime import timedelta
        from ai.settings import env_float
        current = parse_time(now)
        since = current - timedelta(hours=env_float("VIOLET_CONTEXT_SESSION_GAP_HOURS", 3.0))
        rows = [r for r in self.db.fetch_all("SELECT role, content, created_at FROM messages ORDER BY id DESC LIMIT 200")
                if r.get("created_at") and parse_time(r["created_at"]) <= current and WEATHER_WORDS_RE.search(r["content"] or "")]
        if any(r["role"] == "user" and parse_time(r["created_at"]) >= since for r in rows):
            return False                                  # the user brought the weather up in this conversation
        return any(parse_time(r["created_at"]) < since for r in rows)

    # ------------------------------------------------------------------
    # Persona patterns
    # ------------------------------------------------------------------
    def _persona_violations(self, text: str) -> list[Violation]:
        found = []
        call = _owner_call()
        for pattern in self.core.forbidden_backstory():
            match = pattern.search(text)
            # 2026-10-30: with the relationship "女儿" she calls the owner 爸爸 -- "我爸爸" is the owner, not a backstory
            kin = re.sub(r"^我(们家|们|的|和|跟|家)?", "", match.group(0)) if match else ""
            if match and not (call and kin and kin in call and call in text):
                found.append(Violation("forbidden_backstory", f"matches {pattern.pattern}"))
        if not found:
            claim = owned_role_claim(text)                 # phase 9.4 real run: "我们英语老师要是也这样就好了"
            if claim:
                found.append(Violation("forbidden_backstory", f"owns a school / work role: {claim}"))
        for pattern in self.core.identity_violations():
            if pattern.search(text):
                found.append(Violation("identity_conflict", f"matches {pattern.pattern}"))
        return found

    def _user_experiences(self) -> list[str]:
        rows = self.db.fetch_all("SELECT content FROM event_memories WHERE status != 'deleted' AND kind IN ('event', 'legacy')")
        facts = self.db.fetch_all("SELECT value FROM user_profile_facts WHERE status = 'active'")
        return [r["content"] for r in rows] + [f["value"] for f in facts]

    # ------------------------------------------------------------------
    # 1. Life event proposals
    # ------------------------------------------------------------------
    def check_event(self, proposal: dict[str, Any], now: str) -> list[Violation]:
        v: list[Violation] = []
        content = (proposal.get("content") or "").strip()
        now_dt = parse_time(now)
        occurred = parse_time(proposal.get("occurred_at"))
        duration = timedelta(minutes=int(proposal.get("duration_minutes") or 60))
        source = str(proposal.get("source") or "")
        origin = str((proposal.get("source_detail") or {}).get("origin") or "")

        info_kind = str((proposal.get("source_detail") or {}).get("info_kind") or "")
        if origin in EXTERNAL_ORIGINS or source.split(":")[0] in EXTERNAL_ORIGINS or info_kind == "public_fact":
            v.append(Violation("external_as_experience", f"source {source or origin} is external material, not Violet's life"))
        if info_kind == "user_real":
            v.append(Violation("user_experience", "a user's experience is never a life event"))
        if proposal.get("event_type") == "reading_record" or origin == "reading":
            # only "Violet read an article" is hers; what the article says is not (ai/life_interfaces.py)
            from ai.life_interfaces import check_reading_record
            v += [Violation(name, "a reading record may only describe the act of reading, with its source url")
                  for name in check_reading_record(proposal)]
        if not content or occurred is None:
            v.append(Violation("invalid", "content and occurred_at are required"))
            return v
        if occurred > now_dt:
            v.append(Violation("future_event", f"{proposal['occurred_at']} is after now"))

        thread = self.db.fetch_one("SELECT * FROM life_threads WHERE id = ?", (proposal.get("thread_id"),)) \
            if proposal.get("thread_id") else None
        if proposal.get("thread_id") and (not thread or thread["status"] != "active"):
            v.append(Violation("thread_not_active", f"thread {proposal.get('thread_id')} is not active"))
        if thread:
            if occurred < parse_time(thread["started_at"]):
                v.append(Violation("before_thread_start", "event is earlier than its thread"))
            last = self.db.fetch_one("SELECT occurred_at FROM life_events WHERE thread_id = ? AND status = 'happened' "
                                     "ORDER BY occurred_at DESC LIMIT 1", (thread["id"],))
            if last and occurred < parse_time(last["occurred_at"]):
                v.append(Violation("out_of_order", "event is earlier than the thread's latest event"))
            window = timedelta(days=float(self.config.get("duplicate_window_days", 14)))
            for other in self.db.fetch_all("SELECT content, occurred_at FROM life_events WHERE thread_id = ? AND status = 'happened'",
                                           (thread["id"],)):
                if other["content"] == content and abs(parse_time(other["occurred_at"]) - occurred) < window:
                    v.append(Violation("duplicate", "the same thing was just recorded"))
                    break

        for other in self.db.fetch_all("SELECT id, occurred_at, duration_minutes FROM life_events WHERE status = 'happened'"):
            start = parse_time(other["occurred_at"])
            end = start + timedelta(minutes=int(other["duration_minutes"] or 60))
            if occurred < end and start < occurred + duration:
                v.append(Violation("time_conflict", f"overlaps event {other['id']}"))
                break

        plan_id = proposal.get("plan_id")
        if proposal.get("completes_plan"):
            plan = self.db.fetch_one("SELECT * FROM life_plans WHERE id = ?", (plan_id,)) if plan_id else None
            if not plan or plan["status"] not in ("planned", "active") or (thread and plan["thread_id"] != thread["id"]):
                v.append(Violation("plan_not_open", f"plan {plan_id} cannot be completed by this event"))
        elif COMPLETION_RE.search(content) and proposal.get("event_type") != "thread_completed":
            v.append(Violation("completion_without_plan", "says something was finished without resolving a plan"))

        # event text has no subject ("和男朋友一起读…"): it is Violet's own act, so check it as "我…" too
        seen = set()
        for item in self._persona_violations(content) + self._persona_violations("我" + content):
            if item.check not in seen:
                seen.add(item.check)
                v.append(item)
        if DRAMATIC_RE.search(content):
            v.append(Violation("too_dramatic", "ordinary life only"))
        for experience in self._user_experiences():
            if touches_topic(content, experience):
                v.append(Violation("user_experience", "looks like the user's experience, not Violet's"))
                break
        return v

    # ------------------------------------------------------------------
    # 2. Generated replies
    # ------------------------------------------------------------------
    turn_used_web = False          # phase 9.3: set by guard_reply when this turn's Chat run used the web tools
    turn_used_schedule = False     # phase 9.4.2: ... or the timetable tools

    def check_reply(self, text: str, now: str | None = None) -> list[Violation]:
        now = now or self.db.now()
        recent = self.db.fetch_all("SELECT content, occurred_at FROM life_events WHERE status = 'happened' AND is_virtual = 1")
        recent = [e for e in recent if parse_time(e["occurred_at"]) <= parse_time(now)]
        # phase 8.9: "把几首钢琴曲从头听了一遍" is backed by its configured details (德彪西的《月光》…), so naming
        # one of those pieces is not an unrecorded experience
        backs = lambda text, content: mentions_life_strict(text, content, self.config)
        open_plans = self.db.fetch_all("SELECT content FROM life_plans WHERE status IN ('planned', 'active', 'paused')")
        from ai.daily_life import life_terms
        reading_terms = [t for r in self.db.fetch_all("SELECT title FROM life_threads WHERE status = 'active'")
                         for t in life_terms(r["title"], self.config)]
        experiences = self._user_experiences()
        # phase 5B: what web pages say is never Violet's own experience
        finding_titles = [r["title"] for r in self.db.fetch_all(
            "SELECT title FROM web_findings WHERE status = 'active' ORDER BY id DESC LIMIT 60")]
        violations: list[Violation] = []
        stale = None
        for line in [l.strip() for l in (text or "").splitlines() if l.strip()]:
            if leaked_draft(line):                       # phase 8.9.5: every entity -- never meant for the user
                violations.append(Violation("leaked_draft", "the model's drafting notes in the reply", line))
            if self.check_weather and STALE_WEATHER_RE.search(line):
                if stale is None:
                    stale = self._stale_weather(now)
                if stale:
                    violations.append(Violation("stale_weather", "earlier weather stated as now", line))
            for persona in self._persona_violations(line):
                persona.line = line
                violations.append(persona)
            if CAMERA_CLAIM_RE.search(line) and not CAMERA_NEGATED_RE.search(line):
                violations.append(Violation("camera_claim", "an AI-generated picture told as a photo", line))
            if getattr(self.core, "is_ai", False):
                # 2026-10-30: the AI persona's experiences are its real records, not virtual life events; what it must
                # not claim (a body, a family, human days) is in its own forbidden patterns, checked above
                continue
            # hedges ("想", "下次") and references to the user ("你") only count inside the claiming clause:
            # "我昨天去了海边，下次带你去" is still a claim
            claims = _claims(line)
            if not claims:
                continue
            if self.turn_used_web and WEB_ACTION_RE.search(line) and not LIVED_RE.search(line):
                # phase 9.3: "刚查了一下 / 看了原文" on a turn where Violet really searched / read a page is that action,
                # not an unrecorded experience (what the page says is still not hers -- the notice tells Chat so)
                continue
            if self.turn_used_schedule and SCHEDULE_ACTION_RE.search(line) and not LIVED_RE.search(line):
                # phase 9.4.2: the same for looking at the user's timetable this turn
                continue
            as_done = EXPERIENCE_AS_DONE_RE.sub(r"\1了", line)      # records are written in the completed form
            claims_completion = bool(COMPLETION_RE.search(line))
            # a progress event ("读了《边城》的一部分") does not back a claim of finishing it
            if any(backs(as_done, e["content"]) and (not claims_completion or COMPLETION_RE.search(e["content"]))
                   for e in recent):
                continue
            if STATE_CLAIM_RE.search(line) and _state_backed(line, recent, parse_time(now)):
                continue
            # phase 8.9.10: a book she is reading backs reading a part of it -- not finishing it, and not another book
            if not claims_completion and any(t in line for t in reading_terms):
                continue
            if any(mentions_topic(as_done, p["content"]) for p in open_plans):
                violations.append(Violation("plan_described_as_done", "a plan that has not happened", line))
            elif all(USER_REFERENCE_RE.search(c) for c in claims):     # the claimed part itself is about the user
                continue
            elif any(touches_topic(as_done, e) for e in experiences):
                violations.append(Violation("user_experience_as_own", "the user's experience told as Violet's", line))
            elif any(touches_topic(as_done, t) for t in finding_titles):
                violations.append(Violation("external_as_experience", "something read on the web told as lived", line))
            else:
                violations.append(Violation("unrecorded_experience", "no recorded event behind this claim", line))
        return violations

    @staticmethod
    def drop_lines(text: str, violations: list[Violation]) -> str:
        """Phase 8.9.13: a line kept right after a dropped one no longer opens with a word that leans on it -- the real
        ab-v2e reply became just "不过你说的余味，我好像又有点想要了" once its first line was dropped."""
        bad = {v.line for v in violations if v.line}
        kept, dropped_before = [], False
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            if line.strip() in bad:
                dropped_before = True
                continue
            if dropped_before and not kept:
                line = _LEANING_START_RE.sub("", line.strip()) or line
            kept.append(line)
            dropped_before = False
        return "\n".join(kept)
