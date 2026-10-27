"""Violet Control Center settings (phase 9.0): a whitelist of settings the admin may change from the dashboard.

Only settings listed in ``SETTINGS`` exist here -- each with a type, a range, a default and when it takes effect
("now": read on every use or reloaded right after saving; "restart": read once at start). Nothing else from the
environment is shown or editable.

Storage (``<data_dir>/control/``, git-ignored with the rest of data/):
  settings.json   the overridden values (no secrets)
  secrets.json    secret values (API key) and up to 3 previous ones for "restore previous", file mode 0600;
                  a secret is never returned, only "set / not set" and a short fingerprint
  history.jsonl   one line per save: who, why, which keys, old -> new (secrets as fingerprints), and the full
                  non-secret snapshot after the save -- rollback restores such a snapshot

The overrides sit on top of .env: ``reload_env()`` reads .env (override=True, as the agents always did) and then
applies them, so an agent reload never silently drops a value saved here. Removing an override returns the key to
whatever .env (or the code default) says. A save is validated completely before anything is written; the files are
replaced atomically; if applying the new values fails, the previous files are put back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ai.settings import ENV_PATH, PROJECT_ROOT

logger = logging.getLogger("violet.control")

ROLES = (("INTENT", "IntentAgent（理解每句话）"), ("CHAT", "ChatAgent（写回复）"), ("MEMORY", "MemoryAgent（记忆）"),
         ("PROACTIVE", "ProactiveAgent（主动联系决策）"), ("PERFORMER", "PerformerAgent（执行任务）"))
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,119}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PROMPT_DIRS = {"chat": PROJECT_ROOT / "persona" / "chat_prompt_versions",
               "proactive": PROJECT_ROOT / "persona" / "proactive_prompt_versions"}


@dataclass
class Setting:
    key: str
    label: str
    group: str
    kind: str                       # url | secret | model | float | int | bool | hhmm | prompt_version | choice | text
    #                                 | timezone
    effect: str                     # now | restart
    help: str = ""
    default: Any = None
    min: float | None = None
    max: float | None = None
    optional: bool = False          # empty value allowed (model fallback; text: "" = say nothing)
    target: str = "env"             # env | proactive (proactive_config.json) | prompt
    path: tuple = field(default_factory=tuple)
    choices: tuple = ()             # choice: ((value, label), ...)

    def public(self) -> dict[str, Any]:
        out = {k: getattr(self, k) for k in ("key", "label", "group", "kind", "effect", "help", "default", "min", "max",
                                              "optional")}
        if self.kind == "prompt_version":
            out["choices"] = prompt_versions(self.key.split(".")[1])
        elif self.kind == "choice":
            out["choices"] = [{"value": v, "label": label} for v, label in self.choices]
        return out


def _agent_settings() -> list[Setting]:
    out = []
    for role, label in ROLES:
        out += [
            Setting(f"{role}_LLM_MODEL", f"{label} 模型", "models", "model", "now", "模型标识，按中转服务里的名字填写"),
            Setting(f"{role}_LLM_FALLBACK_MODEL", f"{label} 备用模型", "models", "model", "now",
                    "主模型重试用完后各试一次；留空表示不用", optional=True),
            Setting(f"{role}_LLM_MODEL_TEMPERATURE", f"{label} 温度", "models", "float", "now",
                    "代码里只有温度这一个可调的采样参数；max_tokens / reasoning_effort 没有按 Agent 发送", None, 0.0, 2.0),
        ]
    return out


SETTINGS: list[Setting] = [
    Setting("LLM_API_URL", "API 地址", "api", "url", "now", "OpenAI 兼容接口，例如 https://relay.example/v1"),
    Setting("LLM_API_KEY", "API Key", "api", "secret", "now", "只能替换，不能查看；保存后立即用于新的调用"),
    *_agent_settings(),
    Setting("LLM_REQUEST_TIMEOUT_SECONDS", "单次请求超时（秒）", "api", "float", "now", "", 90.0, 5, 600),
    Setting("LLM_CONNECT_TIMEOUT_SECONDS", "连接超时（秒）", "api", "float", "now", "", 10.0, 1, 120),
    Setting("LLM_MAX_RETRY_ATTEMPTS", "最多尝试次数", "api", "int", "now", "包括第一次", 3, 1, 6),
    Setting("LLM_RETRY_BASE_DELAY", "重试基础间隔（秒）", "api", "float", "now", "", 3.0, 0, 60),
    Setting("LLM_TOTAL_TIMEOUT_SECONDS", "一次调用的总时限（秒）", "api", "float", "now", "包括所有重试", 180.0, 10, 1800),
    Setting("prompt.chat", "Chat 提示词版本", "prompts", "prompt_version", "now",
            "留空 = 用 .env 里的 CHAT_LLM_PROMPT；选一个版本 = 用 persona/chat_prompt_versions 里对应的文件",
            target="prompt"),
    Setting("prompt.proactive", "主动联系决策提示词版本", "prompts", "prompt_version", "now",
            "留空 = 用 .env 里的 PROACTIVE_LLM_PROMPT", target="prompt"),
    Setting("DEBOUNCE_NORMAL", "等对方说完再回（秒）", "messages", "float", "now",
            "对方连发几条时，最后一条之后等多久再一起处理", 5.0, 0, 120),
    Setting("DEBOUNCE_EXTRA", "对方像是没说完时多等（秒）", "messages", "float", "now", "", 10.0, 0, 300),
    Setting("VIOLET_SIMULATE_TYPING", "模拟打字间隔", "messages", "bool", "now", "多条消息之间按长度停顿一下", True),
    Setting("VIOLET_TYPING_MIN_DELAY_SECONDS", "打字间隔下限（秒）", "messages", "float", "now", "", 1.0, 0, 30),
    Setting("VIOLET_TYPING_MAX_DELAY_SECONDS", "打字间隔上限（秒）", "messages", "float", "now", "", 20.0, 0, 120),
    Setting("proactive.enabled", "主动联系", "proactive", "bool", "now", "关掉后不再主动找对方（约定的提醒不受影响）",
            True, target="proactive", path=("enabled",)),
    Setting("proactive.quiet_start", "静默时段开始", "proactive", "hhmm", "now", "这段时间不主动联系", "01:00",
            target="proactive", path=("quiet_hours", "start")),
    Setting("proactive.quiet_end", "静默时段结束", "proactive", "hhmm", "now", "", "08:00",
            target="proactive", path=("quiet_hours", "end")),
    Setting("proactive.daily_limit", "每天最多主动几次", "proactive", "int", "now", "", 3, 0, 20,
            target="proactive", path=("daily_message_limit",)),
    Setting("proactive.after_user_minutes", "对方说话后至少隔多久（分钟）", "proactive", "int", "now", "", 90, 0, 1440,
            target="proactive", path=("min_minutes_after_user_message",)),
    Setting("proactive.between_minutes", "两次主动之间至少隔多久（分钟）", "proactive", "int", "now", "", 180, 0, 1440,
            target="proactive", path=("min_minutes_between_proactive_messages",)),
    Setting("proactive.max_cold_streak", "连续几次没回就先等对方", "proactive", "int", "now", "", 2, 1, 10,
            target="proactive", path=("max_cold_streak",)),
    Setting("VIOLET_LIFE_ENGINE", "生活引擎", "life", "bool", "now",
            "关闭后下一次检查起不再记录新的生活进展，打开后马上开始；临时暂停也可以用下面的“暂停 / 恢复”", True),
    Setting("VIOLET_RELATIONSHIP_STATE", "关系阶段描述", "behavior", "bool", "now", "按聊天记录判断熟悉程度并告诉 Chat", True),
    Setting("VIOLET_CONTEXT_HISTORY_TURNS", "聊天历史保留几轮", "behavior", "int", "now", "同一方连发的几行算一轮", 24, 0, 60),
    Setting("VIOLET_REPLY_HABIT_NOTE", "回复习惯提示", "behavior", "bool", "now", "提醒 Chat 最近已经问过、关心过什么", True),
    # phase 10.7: feature switches that used to need .env (the code reads them at every agent (re)load / call)
    Setting("VIOLET_WEB_ENABLED", "联网搜索和网页阅读", "features", "bool", "now",
            "开启后 Violet 在需要实时信息时可以搜索（需要下面的 SearXNG 地址，或在 .env 里配置其他搜索服务）", False),
    Setting("VIOLET_WEB_SEARXNG_URL", "SearXNG 地址", "features", "url", "now",
            "自建 SearXNG 的地址，例如 http://127.0.0.1:18089（部署方法见 deploy/searxng/README.md）", optional=True),
    Setting("VIOLET_SCHEDULE_ENABLED", "课表查询（violet-schedule）", "features", "bool", "now",
            "Violet 可以查你在“课表”页录入的课表", False),
    Setting("VIOLET_SCHEDULE_CONTEXT", "聊天时考虑课表", "features", "bool", "now", "聊天时知道你现在是不是在上课", False),
    Setting("VIOLET_SCHEDULE_PROACTIVE", "主动联系避开上课", "features", "bool", "now",
            "上课、考试时不主动找你（按课表计算）", False),
    # Violet stage 3: everything a user may want to change without editing .env. Left out on purpose: what the
    # installer owns (paths, port, HTTPS, data directory, admin token), stdio MCP commands, test switches.
    Setting("VIOLET_WEB_SEARCH_PROVIDER", "搜索服务", "features", "choice", "now",
            "SearXNG 是自建的开源搜索；博查 / Brave 是付费搜索 API（Key 在“工具”页的联网工具里替换）", "searxng",
            choices=(("searxng", "SearXNG（自建）"), ("bocha", "博查（付费 API）"), ("brave", "Brave Search（付费 API）"),
                     ("none", "不搜索"))),
    Setting("VIOLET_WEB_SEARCH_ENDPOINT", "搜索 API 地址", "features", "url", "now",
            "只对博查 / Brave：换成别的接口地址；留空 = 官方地址", optional=True),
    Setting("VIOLET_WEB_SEARCH_LANGUAGE", "搜索语言", "features", "choice", "now", "SearXNG 搜索结果的语言", "zh-CN",
            choices=(("zh-CN", "简体中文"), ("zh-TW", "繁體中文"), ("en", "English"), ("ja", "日本語"), ("all", "不限"))),
    Setting("VIOLET_WEB_EXPLORER", "允许自主探索", "features", "bool", "now",
            "允许 Violet 在空闲时自己上网看看感兴趣的东西（会用模型、计入联网次数）；打开后还要在“联网与探索”页开启", False),
    Setting("VIOLET_WEB_EXPLORER_LLM_MODEL", "自主探索用的模型", "features", "model", "now", "留空 = 不探索", optional=True),
    Setting("VIOLET_IMAGE_ENABLED", "图片功能", "images", "bool", "now",
            "关闭时收到的图片只用下面的识图模型看一眼，聊天里没有生成 / 编辑图片的工具", False),
    Setting("VISION_LLM_MODEL", "识图模型", "images", "model", "now", "看懂你发来的图片用的模型", "vision"),
    Setting("VISION_TIMEOUT_SECONDS", "识图超时（秒）", "images", "float", "now", "", 60.0, 5, 300),
    Setting("VIOLET_IMAGE_LEGACY_VISION", "没有图片功能时也识图", "images", "bool", "now",
            "图片功能关闭或没有可用的识图服务时，用上面的识图模型", True),
    Setting("VIOLET_IMAGE_LEGACY_VISION_MAX_PER_DAY", "每天最多识图几次", "images", "int", "now", "", 50, 0, 1000),
    Setting("VIOLET_MEDIA_INBOUND_RETENTION_DAYS", "收到的图片保留（天）", "images", "float", "now",
            "到期后删除图片文件（对图片的文字描述另算）", 7.0, 1, 3650),
    Setting("VIOLET_MEDIA_GENERATED_RETENTION_DAYS", "生成的图片保留（天）", "images", "float", "now", "", 30.0, 1, 3650),
    Setting("VIOLET_IMAGE_TEXT_RETENTION_DAYS", "图片描述保留（天）", "images", "float", "now",
            "识图结果、生成图片的提示词保留多久", 30.0, 1, 3650),
    Setting("VIOLET_DAILY_LIFE", "日常作息", "life", "bool", "now",
            "Violet 有自己普通的一天（按日期固定，不用模型），聊天时会自然提到", True),
    Setting("VIOLET_LIFE_GENERATION_MODE", "生活事件的写法", "life", "choice", "now",
            "模型写法：只让模型润色进展的措辞，程序检查后才保存（会用模型）", "template",
            choices=(("template", "模板（不用模型）"), ("llm", "模型润色"))),
    Setting("VIOLET_LIFE_LLM_MODEL", "润色生活事件用的模型", "life", "model", "restart", "留空 = 用模板", optional=True),
    Setting("VIOLET_ASYNC_MEMORY", "回复后再整理记忆", "memory", "bool", "restart",
            "关闭后先整理记忆再回复（回复会慢一些）", True),
    Setting("VIOLET_MEMORY_WORD_RECALL", "按关键词带出旧记忆", "memory", "bool", "now",
            "你这句话和某条记忆有相同的关键词时，直接让 Chat 看到这条记忆", True),
    Setting("VIOLET_EVENT_VALID_DAYS", "事件记忆有效期（天）", "memory", "float", "now",
            "过期只标记为过期，不删除", 14.0, 1, 3650),
    Setting("VIOLET_PROFILE_STORE_SENSITIVE", "自动记住敏感信息", "memory", "bool", "now",
            "健康、财务、证件号、联系方式、精确住址等默认不自动保存", False),
    Setting("VIOLET_REMINDER_BEFORE_REPLY", "先设提醒再回复", "memory", "bool", "now",
            "“提醒我……”在回复前就保存，回复里如实说有没有设成", True),
    Setting("VIOLET_COMMITMENT_DEFAULT_TIME", "提醒的默认时刻", "memory", "hhmm", "now", "只说了日期没说时间时用", "09:00"),
    Setting("VIOLET_ONBOARDING", "慢慢认识你", "behavior", "bool", "now",
            "聊天中偶尔自然地问一个关于你的问题（每次最多一个，被拒绝的不再问）", True),
    Setting("VIOLET_FOLLOW_UP_CHECK", "少追问", "behavior", "bool", "now",
            "你说过不喜欢被追问时，回复仍以提问结尾就重写一次（多一次模型调用）", True),
    Setting("VIOLET_PERSONA_CONTEXT", "人设和生活进入对话", "behavior", "bool", "now", "", True),
    Setting("VIOLET_PROACTIVE_TOPIC_GUARD", "主动消息避开不该提的话题", "behavior", "bool", "now",
            "主动消息谈到已结束的事、冷却中的话题时不发送", True),
    Setting("VIOLET_PERSONA_REPLY_GUARD", "回复一致性检查", "guard", "choice", "now",
            "检查回复有没有和人设、生活记录矛盾", "enforce",
            choices=(("enforce", "发现矛盾就改写"), ("flag", "只记录"), ("off", "关闭"))),
    Setting("VIOLET_SEMANTIC_GUARD", "模型复核", "guard", "choice", "now",
            "规则判断不了的“自己经历”句子，再请模型复核一次（会用模型）", "off",
            choices=(("off", "关闭"), ("on", "开启"))),
    Setting("VIOLET_SEMANTIC_GUARD_MODEL", "复核用的模型", "guard", "model", "restart", "留空 = 不复核", optional=True),
    Setting("VIOLET_SEMANTIC_GUARD_MAX_CALLS_PER_DAY", "复核每天最多几次", "guard", "int", "restart", "", 60, 0, 10000),
    Setting("VIOLET_SEMANTIC_GUARD_MAX_TOKENS_PER_DAY", "复核每天最多用多少 token", "guard", "int", "restart", "",
            60000, 0, 10_000_000),
    Setting("MCP_CONNECT_TIMEOUT_SECONDS", "连接超时（秒）", "mcp", "float", "now", "连接 MCP 服务器（例如手机）", 8.0, 1, 120),
    Setting("MCP_CALL_TIMEOUT_SECONDS", "工具调用超时（秒）", "mcp", "float", "now", "", 30.0, 1, 600),
    Setting("MCP_CALL_RETRIES", "断线重试次数", "mcp", "int", "now", "只对连接中途断开重试；超时、设备离线不重试", 1, 0, 5),
    Setting("MCP_RESULT_MAX_CHARS", "工具结果最长（字）", "mcp", "int", "now", "更长的结果会截断后再给模型", 6000, 500, 100000),
    Setting("VIOLET_SEND_MAX_ATTEMPTS", "发送失败重试次数", "messages", "int", "restart",
            "只对“消息没送到聊天平台”的连接错误重试", 2, 1, 10),
    Setting("VIOLET_SEND_RETRY_DELAY_SECONDS", "发送重试间隔（秒）", "messages", "float", "restart", "", 2.0, 0, 60),
    Setting("VIOLET_FALLBACK_REPLY", "出错时的兜底回复", "messages", "text", "now",
            "模型整轮失败时发给你的一句话；留空 = 不发（错误仍会记录）",
            "我这边刚刚卡了一下，没接上你的话，你再说一次好不好", optional=True),
    Setting("VIOLET_STARTUP_MESSAGE", "启动通知", "system", "text", "restart",
            "Violet 启动后发给你的一句话；留空 = 不发", "", optional=True),
    Setting("VIOLET_TIMEZONE", "时区", "system", "timezone", "restart",
            "提醒、静默时段、课表和每天 03:00 的记忆整理都按这个时区（IANA 名称，例如 Asia/Shanghai）", "Asia/Shanghai"),
    Setting("VIOLET_LOG_LEVEL", "日志详细程度", "system", "choice", "restart", "日志里从不记录 API Key 和密码", "INFO",
            choices=(("WARNING", "只记问题"), ("INFO", "标准"), ("DEBUG", "详细（排查问题时用）"))),
    Setting("VIOLET_DASHBOARD_IDLE_MINUTES", "后台无操作多久要重新登录（分钟）", "system", "int", "restart", "", 120, 5, 1440),
    Setting("VIOLET_DASHBOARD_SESSION_HOURS", "后台登录最长保持（小时）", "system", "int", "restart", "", 12, 1, 720),
    Setting("VIOLET_UPDATE_MODE", "更新方式", "updates", "choice", "now",
            "自动更新：发现新版本就在服务器上自动升级（先备份，失败自动退回）；只提醒：自动检查，升级要你点“立即更新”"
            "或执行命令；手动：只在你点“检查更新”时才查", "auto",
            choices=(("auto", "自动更新"), ("notify", "自动检查，只提醒"), ("manual", "手动检查"))),
]
BY_KEY = {s.key: s for s in SETTINGS}
GROUPS = {"api": "接口", "models": "模型与 Agent", "prompts": "提示词版本", "messages": "消息发送", "proactive": "主动联系",
          "life": "生活引擎", "behavior": "聊天行为", "memory": "记忆与提醒", "guard": "回复检查", "features": "功能开关",
          "images": "图片", "mcp": "MCP 工具调用", "system": "系统", "updates": "版本更新"}
TEXT_MAX = 200


def prompt_versions(kind: str) -> list[str]:
    folder = PROMPT_DIRS.get(kind)
    prefix = f"{kind}."
    if not folder or not folder.is_dir():
        return []
    return sorted(p.stem[len(prefix):] for p in folder.glob(f"{prefix}v*.txt"))


def fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(("violet-control:" + value).encode("utf-8")).hexdigest()[:10] if value else ""


class StoreError(RuntimeError):
    """The settings files could not be read safely; nothing was saved."""


class ValidationError(ValueError):
    def __init__(self, problems: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems


def validate(key: str, value: Any) -> Any:
    """The value to store, or ValueError with a reason. None / "" removes the override (back to .env / default)."""
    s = BY_KEY.get(key)
    if s is None:
        raise ValueError("不允许修改这个设置")
    if value is None or (isinstance(value, str) and value.strip() == "" and s.kind not in ("model", "text")):
        return None
    if s.kind == "text":
        value = str(value).strip()
        if not value and not s.optional:
            raise ValueError("不能为空")
        if len(value) > TEXT_MAX or any(ord(c) < 32 for c in value):
            raise ValueError(f"最多 {TEXT_MAX} 个字，只能写一行")
        return value
    if s.kind == "choice":
        value = str(value).strip()
        if value not in {v for v, _label in s.choices}:
            raise ValueError("没有这个选项")
        return value
    if s.kind == "timezone":
        value = str(value).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}", value) or len(value) > 64:
            raise ValueError("请填写 IANA 时区名称，例如 Asia/Shanghai")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(value)
        except Exception:
            raise ValueError("没有这个时区（例如 Asia/Shanghai、Europe/London、America/New_York）")
        return value
    if s.kind == "secret":
        value = str(value).strip()
        if len(value) < 8 or len(value) > 512 or any(c.isspace() for c in value):
            raise ValueError("Key 的长度或格式不对")
        return value
    if s.kind == "url":
        value = str(value).strip()
        if not re.match(r"^https?://[^\s/?#@]+(/[^\s?#@]*)?$", value) or len(value) > 300:
            # no query string and no user:password@ -- a credential must never end up in the change history
            raise ValueError("请填写 http:// 或 https:// 开头的地址，不带查询参数，也不能在地址里写用户名或密码")
        return value.rstrip("/")
    if s.kind == "model":
        value = str(value).strip()
        if not value:
            if s.optional:
                return ""
            raise ValueError("模型不能为空")
        if not MODEL_RE.match(value):
            raise ValueError("模型标识只能包含字母、数字和 . _ : / @ + -")
        return value
    if s.kind in ("float", "int"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("请填数字")
        if isinstance(value, bool) or not math.isfinite(number):      # NaN passes every range check; inf has no int
            raise ValueError("请填数字")
        if s.kind == "int":
            if number != int(number):
                raise ValueError("请填整数")
            number = int(number)
        if (s.min is not None and number < s.min) or (s.max is not None and number > s.max):
            raise ValueError(f"范围是 {s.min:g}–{s.max:g}")
        return number
    if s.kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError("请选择开或关")
    if s.kind == "hhmm":
        value = str(value).strip()
        if not HHMM_RE.match(value):
            raise ValueError("格式是 HH:MM")
        return value
    if s.kind == "prompt_version":
        value = str(value).strip()
        if value not in prompt_versions(key.split(".")[1]):
            raise ValueError("没有这个提示词版本")
        return value
    raise ValueError("未知类型")


def _env_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value == int(value):
        return str(int(value))                                   # 4.0 -> "4", as it would be written in .env
    return str(value)


class ControlStore:
    """settings.json / secrets.json / history.jsonl under <data_dir>/control."""

    _lock = threading.RLock()

    def __init__(self, data_dir: str | os.PathLike[str] | None = None):
        base = Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data")
        self.dir = base / "control"
        self.settings_path = self.dir / "settings.json"
        self.secrets_path = self.dir / "secrets.json"
        self.history_path = self.dir / "history.jsonl"

    # -- files
    def _read(self, path: Path, strict: bool = False) -> dict:
        """The file's content. Unreadable: at run time the overrides are skipped (.env stays in effect); when saving
        (strict) nothing is written, so a damaged file is never silently replaced by a partial one."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            pass
        logger.error("control settings file %s is unreadable; .env values stay in effect", path)
        if strict:
            raise StoreError(f"{path.name} 无法读取，为了不覆盖它，这次没有保存")
        return {}

    def _write(self, path: Path, data: dict, secret: bool = False) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{_secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            if secret:
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def _restore(self, path: Path, raw: bytes | None) -> None:
        """Put a file back exactly as it was (or remove it if it did not exist)."""
        if raw is None:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_name(path.name + f".{_secrets.token_hex(4)}.tmp")
        try:
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _raw(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def values(self) -> dict[str, Any]:
        return dict(self._read(self.settings_path).get("values") or {})

    def secret_values(self) -> dict[str, str]:
        return dict(self._read(self.secrets_path).get("values") or {})

    def revision(self) -> int:
        return int(self._read(self.settings_path).get("revision") or 0)

    def history(self, limit: int = 50) -> list[dict]:
        if not self.history_path.exists():
            return []
        rows = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows[-limit:][::-1]

    # -- saving
    def save(self, changes: dict[str, Any], actor: str, reason: str = "",
             apply: Callable[[], None] | None = None, snapshot: dict | None = None) -> dict:
        """Validate every change, then write both files, apply and append the history line as one unit: if any step
        fails, both files are put back byte for byte, the previous values are applied again and nothing is recorded."""
        problems, cleaned = {}, {}
        for key, value in (changes or {}).items():
            try:
                cleaned[key] = validate(key, value)
            except ValueError as exc:
                problems[key] = str(exc)
        if problems:
            raise ValidationError(problems)
        with self._lock:
            settings_doc = self._read(self.settings_path, strict=True)
            secrets_doc = self._read(self.secrets_path, strict=True)
            raw_settings, raw_secrets = self._raw(self.settings_path), self._raw(self.secrets_path)
            raw_history = self._raw(self.history_path)
            values = dict(snapshot) if snapshot is not None else dict(settings_doc.get("values") or {})
            secret_values = dict(secrets_doc.get("values") or {})
            previous = dict(secrets_doc.get("previous") or {})
            record = []
            for key, value in cleaned.items():
                s = BY_KEY[key]
                if s.kind == "secret":
                    old = secret_values.get(key)
                    if value is None:
                        secret_values.pop(key, None)
                    else:
                        secret_values[key] = value
                    if old and old != value:
                        previous[key] = ([old] + [p for p in previous.get(key, []) if p != old])[:3]
                    record.append({"key": key, "old": fingerprint(old or ""), "new": fingerprint(value or "")})
                else:
                    old = values.get(key)
                    if value is None:
                        values.pop(key, None)
                    else:
                        values[key] = value
                    record.append({"key": key, "old": old, "new": value})
            if snapshot is not None:                      # rollback: record exactly what differs from before
                old_values = settings_doc.get("values") or {}
                record = [{"key": k, "old": old_values.get(k), "new": values.get(k)}
                          for k in sorted(set(old_values) | set(values)) if old_values.get(k) != values.get(k)]
            revision = int(settings_doc.get("revision") or 0) + 1
            entry = {"revision": revision, "at": int(time.time()), "actor": actor, "reason": (reason or "")[:200],
                     "changes": record, "snapshot": values}
            try:
                self._write(self.settings_path, {"revision": revision, "values": values, "updated_at": int(time.time())})
                self._write(self.secrets_path, {"values": secret_values, "previous": previous}, secret=True)
                if apply is not None:
                    apply()
                with self.history_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                logger.exception("saving control settings failed; the previous settings were put back")
                for path, raw in ((self.settings_path, raw_settings), (self.secrets_path, raw_secrets),
                                  (self.history_path, raw_history)):
                    try:
                        self._restore(path, raw)
                    except Exception:
                        logger.exception("restoring %s failed", path.name)
                if apply is not None:
                    try:
                        apply()
                    except Exception:
                        logger.exception("re-applying the previous control settings failed")
                raise
            return {"revision": revision, "changes": record}

    def rollback(self, revision: int, actor: str, apply: Callable[[], None] | None = None) -> dict:
        """Put back the non-secret settings exactly as they were right after ``revision``. Secrets are not touched."""
        entry = next((h for h in self.history(limit=10_000) if h.get("revision") == revision), None)
        if entry is None:
            raise ValueError("没有这个修改记录")
        snapshot = {k: v for k, v in (entry.get("snapshot") or {}).items() if k in BY_KEY}
        problems = {}
        for key, value in list(snapshot.items()):          # history.jsonl is a file: re-check before trusting it
            try:
                snapshot[key] = validate(key, value)
            except ValueError as exc:
                problems[key] = str(exc)
        if problems:
            raise ValidationError(problems)
        snapshot = {k: v for k, v in snapshot.items() if v is not None}
        return self.save({}, actor, f"回滚到第 {revision} 次修改", apply=apply, snapshot=snapshot)

    def restore_previous_secret(self, key: str, actor: str, apply: Callable[[], None] | None = None) -> dict:
        """The value before the last replacement. If the dashboard replaced a key that came from .env (no earlier
        dashboard value), "previous" is the .env key: the override is removed (phase 9.0.1)."""
        doc = self._read(self.secrets_path, strict=True)
        previous = (doc.get("previous") or {}).get(key) or []
        current = (doc.get("values") or {}).get(key)
        if previous:
            return self.save({key: previous[0]}, actor, "恢复上一个值", apply=apply)
        if current:
            return self.save({key: None}, actor, "恢复为 .env 里的值", apply=apply)
        raise ValueError("没有可以恢复的旧值")

    # -- applying
    def environ_values(self) -> dict[str, str]:
        """Environment variable -> value for every override (prompt versions become the prompt text)."""
        out: dict[str, str] = {}
        for key, value in self.values().items():
            s = BY_KEY.get(key)
            if s is None:
                continue
            if s.target == "env":
                out[key] = _env_text(value)
            elif s.target == "prompt":
                kind = key.split(".")[1]
                path = PROMPT_DIRS[kind] / f"{kind}.{value}.txt"
                if path.is_file():
                    out[f"{kind.upper()}_LLM_PROMPT"] = path.read_text(encoding="utf-8")
        for key, value in self.secret_values().items():
            if key in BY_KEY:
                out[key] = value
        return out

    def apply_to_environ(self, environ: dict | None = None) -> None:
        """Set every override. A variable this store set before but no longer overrides goes back to .env, or to
        the value the process had before the first override, or is removed -- never left at the old override."""
        environ = os.environ if environ is None else environ
        wanted = self.environ_values()
        with self._lock:
            originals = _ORIGINALS.setdefault(id(environ), {})
            dotenv = None
            for name in [n for n in originals if n not in wanted]:
                if dotenv is None:
                    dotenv = _dotenv_values()
                original = originals.pop(name)
                if name in dotenv:
                    environ[name] = dotenv[name]
                elif original is not None:
                    environ[name] = original
                else:
                    environ.pop(name, None)
            for name, value in wanted.items():
                if name not in originals:
                    originals[name] = environ.get(name)
                environ[name] = value

    def proactive_overrides(self) -> dict:
        out: dict = {}
        for key, value in self.values().items():
            s = BY_KEY.get(key)
            if s is None or s.target != "proactive":
                continue
            node = out
            for part in s.path[:-1]:
                node = node.setdefault(part, {})
            node[s.path[-1]] = value
        return out

    # -- reading for the page
    def describe(self, runtime: dict[str, Any] | None = None) -> list[dict]:
        """Every setting with the value Violet is USING now (phase 9.0.1): ``runtime`` holds what the running objects
        report (agent configs, the scheduler's merged proactive config, the prompt version in use); everything else
        is read from the process environment exactly the way the code reads it (env_float / env_int / env_bool with
        the code default). ``saved`` is the dashboard override, if any."""
        from ai.settings import env_bool, env_float, env_int
        runtime = runtime or {}
        values, secret_values = self.values(), self.secret_values()
        out = []
        for s in SETTINGS:
            row = s.public()
            row["saved"] = values.get(s.key)
            if s.kind == "secret":
                current = os.environ.get(s.key, "")
                row.update(value=None, saved=None, is_set=bool(current), fingerprint=fingerprint(current),
                           source="dashboard" if s.key in secret_values else ("env" if current else "unset"))
            elif s.key in runtime:
                row.update(value=runtime[s.key], source="dashboard" if s.key in values else
                           ("file" if s.target == "proactive" else ("env" if s.target == "prompt" or s.key in os.environ
                                                                    else "default")))
            elif s.target == "env":
                if s.kind == "float":
                    value = env_float(s.key, s.default)
                elif s.kind == "int":
                    value = env_int(s.key, s.default)
                elif s.kind == "bool":
                    value = env_bool(s.key, s.default)
                elif s.kind == "text":
                    value = os.environ[s.key].strip() if s.key in os.environ else s.default   # "" is a real value
                else:
                    value = (os.environ.get(s.key) or "").strip() or s.default
                row.update(value=value, source="dashboard" if s.key in values else
                           ("env" if s.key in os.environ else "default"))
            elif s.target == "prompt":
                row.update(value=values.get(s.key, ""), source="dashboard" if s.key in values else "env")
            else:
                row.update(value=values.get(s.key, s.default), source="dashboard" if s.key in values else "file")
            out.append(row)
        return out


def _dotenv_values() -> dict[str, str]:
    try:
        from dotenv import dotenv_values
        return {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}
    except Exception:
        return {}


_DEFAULT_STORE: ControlStore | None = None
_ORIGINALS: dict[int, dict[str, str | None]] = {}     # per environ: value before this module first overrode a name


def store() -> ControlStore:
    global _DEFAULT_STORE
    data_dir = os.environ.get("VIOLET_DATA_DIR") or "./data"
    if _DEFAULT_STORE is None or Path(data_dir) / "control" != _DEFAULT_STORE.dir:
        _DEFAULT_STORE = ControlStore(data_dir)
    return _DEFAULT_STORE


def apply_overrides() -> None:
    """Put the dashboard's settings on top of the environment (after .env was read). Never raises."""
    try:
        store().apply_to_environ()
    except Exception:
        logger.exception("applying control center settings failed; .env values stay in effect")


def reload_env() -> None:
    """Read .env again (override=True, as before) and put the dashboard's settings back on top."""
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    apply_overrides()
