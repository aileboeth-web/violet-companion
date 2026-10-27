"""Vocabulary of the user profile, situations and interaction preferences (phase 3).

Everything here is declarative; the stores (ai/profile_store.py) enforce it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Stable profile facts
# ---------------------------------------------------------------------------

CATEGORY_LABELS = {
    "address": "称呼",
    "identity": "身份背景",
    "study_work": "学习/工作",
    "routine": "生活规律",
    "interest": "兴趣爱好",
    "boundary": "边界",
    "other": "其他",
}

_STUDY_WORDS = ("学校", "大学", "上学", "上课", "下课", "课", "作业", "考试", "期末", "期中", "老师", "同学", "宿舍",
                "室友", "食堂", "图书馆", "实验", "论文", "专业", "毕业", "学期", "开学", "放假", "实习", "社团")
_WORK_WORDS = ("工作", "上班", "下班", "公司", "同事", "老板", "加班", "项目", "开会", "实习", "面试", "辞职")
_HOME_WORDS = ("住", "宿舍", "室友", "回家", "家里", "租房", "学校", "寝室", "搬")
_ROUTINE_WORDS = ("早上", "起床", "睡觉", "熬夜", "作息", "上课", "下课", "下班", "周末", "晚上", "中午", "每天")


@dataclass(frozen=True)
class FactKeySpec:
    key: str
    category: str
    label: str
    multi: bool = False
    sensitivity: str = "normal"          # normal | personal | sensitive
    always_relevant: bool = False        # small facts that shape every reply
    topics: tuple[str, ...] = ()         # message keywords that make the fact relevant
    ask_order: int | None = None         # optional onboarding question order
    ask_hint: str = ""
    # Phase 8.8 (natural context): how a user asks about this fact ("我学什么的"); makes it relevant too.
    asked_as: tuple[str, ...] = ()


PROFILE_KEYS: dict[str, FactKeySpec] = {s.key: s for s in [
    FactKeySpec("address_name", "address", "希望被称呼为", always_relevant=True, topics=("叫我", "称呼", "名字", "昵称"),
                ask_order=1, ask_hint="对方希望你怎么称呼他/她"),
    FactKeySpec("identity.status", "identity", "身份", topics=_STUDY_WORDS + _WORK_WORDS,
                ask_order=2, ask_hint="对方现在是在上学还是已经工作（只了解大概，不追问细节）"),
    FactKeySpec("identity.background", "identity", "背景", multi=True, topics=("老家", "家乡", "年纪", "岁", "小时候")),
    FactKeySpec("study.school", "study_work", "学校", sensitivity="personal", topics=_STUDY_WORDS,
                asked_as=("哪个学校", "哪所学校", "在哪上学", "在哪读书")),
    FactKeySpec("study.major", "study_work", "专业", topics=("专业", "课", "论文", "考试", "作业", "实验"),
                asked_as=("学什么", "学的什么", "学的是", "学啥", "读什么", "读的什么", "读的是", "什么系", "哪个系")),
    FactKeySpec("study.grade", "study_work", "年级", topics=("大一", "大二", "大三", "大四", "研一", "研二", "年级", "毕业", "学期"),
                asked_as=("大几", "几年级", "读几年")),
    FactKeySpec("work.role", "study_work", "工作", topics=_WORK_WORDS,
                asked_as=("做什么的", "干什么的", "做什么工作", "干什么工作", "干啥的")),
    FactKeySpec("residence.usual", "routine", "平时住", sensitivity="personal", topics=_HOME_WORDS,
                ask_order=3, ask_hint="对方平时住在哪种地方（比如学校宿舍还是家里，只问习惯，不问具体地址）",
                asked_as=("住哪", "住在哪")),
    FactKeySpec("routine.schedule", "routine", "生活规律", multi=True, topics=_ROUTINE_WORDS),
    FactKeySpec("interest", "interest", "兴趣", multi=True, ask_order=4, ask_hint="对方平时喜欢做些什么"),
    FactKeySpec("boundary", "boundary", "边界", multi=True, always_relevant=True),
    FactKeySpec("other", "other", "其他", multi=True),
]}

ASK_ORDER = sorted((s for s in PROFILE_KEYS.values() if s.ask_order), key=lambda s: s.ask_order)

# Never auto-persisted (VIOLET_PROFILE_STORE_SENSITIVE=0, the default).
SENSITIVE_KEY_PREFIXES = ("health", "finance", "religion", "politics", "sexuality", "contact", "id", "address.exact")
_SENSITIVE_VALUE_RE = re.compile(
    r"(1[3-9]\d{9}"                                   # mainland mobile number
    r"|\d{17}[\dXx]"                                  # ID card number
    r"|[\w.+-]+@[\w-]+\.[\w.]+"                       # e-mail
    r"|\d{16,19}"                                     # bank card
    r"|\d+\s*(号楼|栋|单元|室|号院|弄)"                # precise address
    r"|病|抑郁|焦虑症|确诊|药物|怀孕|宗教|信仰|政治|性取向|存款|负债|工资\d)"
)


def spec_for(key: str) -> FactKeySpec | None:
    return PROFILE_KEYS.get(key)


def is_sensitive(key: str, value: str) -> bool:
    if any(key == p or key.startswith(p + ".") for p in SENSITIVE_KEY_PREFIXES):
        return True
    return bool(_SENSITIVE_VALUE_RE.search(value or ""))


def sensitivity_for(key: str, value: str) -> str:
    if is_sensitive(key, value):
        return "sensitive"
    spec = spec_for(key)
    return spec.sensitivity if spec else "normal"


# ---------------------------------------------------------------------------
# Interaction preferences
# ---------------------------------------------------------------------------

PREF_VALUES: dict[str, tuple[str, ...] | None] = {
    "reply_length": ("short", "normal", "long"),
    "proactive_chat": ("welcome", "less", "no"),
    "follow_up_questions": ("ok", "less", "no"),
    "advice": ("welcome", "only_when_asked", "no"),
    "humor": ("likes", "neutral", "no"),
    "avoid_topic": None,                       # free text, multi-valued
}
PREF_LABELS = {
    "reply_length": {"short": "喜欢简短的回复", "normal": "回复长短适中", "long": "可以多说一些"},
    "proactive_chat": {"welcome": "欢迎你主动找他/她聊天", "less": "希望你少主动发消息", "no": "不希望你主动发消息"},
    "follow_up_questions": {"ok": "不介意被追问", "less": "不喜欢被连续追问", "no": "不希望被追问"},
    "advice": {"welcome": "愿意听建议", "only_when_asked": "只在他/她问的时候才给建议", "no": "不想要建议"},
    "humor": {"likes": "喜欢开玩笑", "neutral": "玩笑适度", "no": "不喜欢开玩笑"},
}
# Preferences that may be learnt from behaviour (low confidence, many observations).
IMPLICIT_PREF_KEYS = ("proactive_chat", "follow_up_questions")


# ---------------------------------------------------------------------------
# Situations (short-lived)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SituationSpec:
    kind: str
    label: str
    default_ttl_hours: float
    user_only: bool = True                   # only from the user's own words / dashboard
    sensitive: bool = False
    external_sources: tuple[str, ...] = field(default=())


SITUATION_KINDS: dict[str, SituationSpec] = {s.kind: s for s in [
    SituationSpec("location", "此刻所在", 6),             # never from tools or vehicles
    SituationSpec("activity", "正在做", 3),
    SituationSpec("availability", "是否方便", 3),          # busy / sleeping / free
    SituationSpec("mood", "心情", 6),
    SituationSpec("health", "身体状况", 12, sensitive=True),
    SituationSpec("device", "手机状态", 0.5, user_only=False, external_sources=("phone_mcp",)),
]}
BUSY_VALUES_RE = re.compile(r"(忙|开会|考试|上课|睡|勿扰|没空|不方便|busy|sleep)", re.IGNORECASE)
EXTERNAL_MAX_CONFIDENCE = 0.5
EXTERNAL_MAX_TTL_HOURS = 0.5
