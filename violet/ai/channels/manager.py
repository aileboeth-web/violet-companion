"""Channel configuration and lifecycle (phase 10).

Storage (<data_dir>/control/, next to the other Control Center files):
  channels.json            every added channel: type, label, enabled, accept policy, non-secret config; and which
                           channel proactive messages use ("auto" = where the owner last talked)
  channel_secrets.json     tokens and webhook secrets (file mode 0600); never returned, only "set + fingerprint"
  channels_history.jsonl   who changed what (secrets as fingerprints)

WeChat iLink is always there as channel "wechat" (its login is the QR code on the dashboard); it can be switched off.
Other channels are added in the Control Center. A change is validated completely before anything is written; the
channel is then (re)started with the new settings -- no restart of Violet.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ai.channels.base import ChannelAdapter
from ai.channels.context import ChannelContext, label_for

logger = logging.getLogger("violet.channels")

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
WECHAT = "wechat"


def _fingerprint(value: str | None) -> str:
    return hashlib.sha256(("violet-channel:" + value).encode("utf-8")).hexdigest()[:10] if value else ""


def _host_kind(url: str) -> str:
    """loopback | private | public | invalid"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return "invalid"
    if not host:
        return "invalid"
    if host == "localhost":
        return "loopback"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "public"
    if ip.is_loopback:
        return "loopback"
    return "private" if ip.is_private else "public"


def _url_problem(url: Any, allow_private_http: bool) -> str | None:
    if not isinstance(url, str) or not re.match(r"^https?://[^\s/?#@]+(/[^\s?#@]*)?$", url.strip()) or len(url) > 300:
        return "地址要以 http:// 或 https:// 开头，不能带用户名、密码或查询参数"
    kind = _host_kind(url)
    if url.startswith("https://"):
        return None
    if kind == "loopback" or (allow_private_http and kind == "private"):
        return None
    return "不在本机（或局域网）的地址必须用 https"


# type -> (fields of config with validators, secret names {name: (required, validator)})
def _int_field(low: int, high: int) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or not low <= value <= high:
            raise ValueError(f"要填 {low}–{high} 的整数")
        return int(value)
    return check


def _bool_field(value: Any) -> Any:
    if not isinstance(value, bool):
        raise ValueError("要用 true / false")
    return value


def _url_field(allow_private_http: bool) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        problem = _url_problem(value, allow_private_http)
        if problem:
            raise ValueError(problem)
        return value.strip().rstrip("/")
    return check


def _proxy_field(value: Any) -> Any:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.match(r"^(https?|socks5h?)://[^\s@/]+(:\d+)?/?$", value.strip()):
        raise ValueError("代理地址形如 http://127.0.0.1:7890（不能带用户名密码）")
    return value.strip()


def _token_field(pattern: re.Pattern | None, message: str) -> Callable[[Any], Any]:
    def check(value: Any) -> Any:
        if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(c.isspace() for c in value.strip()):
            raise ValueError(message)
        if pattern is not None and not pattern.match(value.strip()):
            raise ValueError(message)
        return value.strip()
    return check


def _app_id_field(value: Any) -> Any:
    from ai.channels.qqbot import APPID_RE
    if not isinstance(value, (str, int)) or not APPID_RE.match(str(value).strip()):
        raise ValueError("AppID 是 QQ 开放平台“开发设置”里的一串数字")
    return str(value).strip()


def channel_types() -> dict[str, dict[str, Any]]:
    from ai.channels.onebot11 import OneBotChannel
    from ai.channels.qqbot import QQBotChannel
    from ai.channels.telegram import TOKEN_RE, TelegramChannel
    return {
        "qqbot": {
            "class": QQBotChannel, "label": "QQ 官方机器人",
            "config": {"app_id": _app_id_field, "sandbox": _bool_field},
            "defaults": {"app_id": "", "sandbox": False},
            "required_config": ["app_id"],
            "secrets": {"app_secret": (True, _token_field(re.compile(r"^[A-Za-z0-9]{8,128}$"),
                                                          "AppSecret 是 QQ 开放平台“开发设置”里的密钥（字母和数字）"))},
            "help": "QQ 开放平台（q.qq.com）创建机器人后，在“开发设置”里找到 AppID 和 AppSecret 填在这里，并在“沙箱配置”"
                    "或正式上线后把你的 QQ 加为可用成员。不需要 NapCat。添加后在后台生成绑定码，用你的 QQ 给机器人发“/bind 绑定码”。"
                    "注意：QQ 只允许在你发消息后 60 分钟内回复，之后 Violet 主动发的消息可能被 QQ 拒绝。",
        },
        "telegram": {
            "class": TelegramChannel, "label": "Telegram",
            "config": {"api_base": _url_field(False), "poll_timeout": _int_field(1, 50), "proxy": _proxy_field},
            "defaults": {"api_base": "https://api.telegram.org", "poll_timeout": 25, "proxy": ""},
            "secrets": {"bot_token": (True, _token_field(TOKEN_RE, "Bot Token 的格式是 数字:字母串（从 @BotFather 获得）"))},
            "help": "在 Telegram 里找 @BotFather 创建机器人，把 Token 填在这里。添加后在后台生成绑定码，用你自己的账号给机器人发“/bind 绑定码”。",
        },
        "onebot11": {
            "class": OneBotChannel, "label": "QQ（OneBot v11，可选，需要 NapCat 等）",
            "config": {"api_base": _url_field(True), "webhook_loopback_only": _bool_field,
                       "check_interval": _int_field(10, 600)},
            "defaults": {"api_base": "http://127.0.0.1:3000", "webhook_loopback_only": True, "check_interval": 30},
            "secrets": {"access_token": (False, _token_field(None, "访问令牌不能为空、不能有空格")),
                        "webhook_secret": (True, _token_field(re.compile(r"^.{16,}$"), "签名密钥至少 16 个字符"))},
            "help": "QQ 通过 NapCat 等 OneBot v11 实现接入：在 NapCat 里开启 HTTP 服务（填到“接口地址”）和 HTTP 上报"
                    "（上报地址填后台显示的 Webhook 地址，签名密钥填这里的密钥）。",
        },
    }


class ChannelStore:
    _lock = threading.RLock()

    def __init__(self, data_dir: str | os.PathLike[str] | None = None):
        self.dir = Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data") / "control"
        self.path = self.dir / "channels.json"
        self.secrets_path = self.dir / "channel_secrets.json"
        self.history_path = self.dir / "channels_history.jsonl"

    def _read(self, path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.error("channel settings file %s is unreadable; channels from it are not started", path)
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
            tmp.unlink(missing_ok=True)

    def doc(self) -> dict:
        return self._read(self.path)

    def channels(self) -> dict[str, dict]:
        return dict(self.doc().get("channels") or {})

    def secrets(self, channel_id: str) -> dict[str, str]:
        return dict((self._read(self.secrets_path).get("channels") or {}).get(channel_id) or {})

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

    def _log(self, actor: str, action: str, channel_id: str, changes: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": int(time.time()), "actor": actor, "action": action, "channel": channel_id,
                                "changes": changes}, ensure_ascii=False) + "\n")

    def save(self, channel_id: str, entry: dict | None, secrets: dict[str, str | None] | None, actor: str,
             action: str) -> None:
        """Write one channel (entry None = remove it) and its secrets in one step, then log. On a failure both files
        are put back."""
        with self._lock:
            doc, sdoc = self.doc(), self._read(self.secrets_path)
            raw = [self._raw(self.path), self._raw(self.secrets_path)]
            channels = dict(doc.get("channels") or {})
            all_secrets = dict(sdoc.get("channels") or {})
            old_entry = channels.get(channel_id)
            current = dict(all_secrets.get(channel_id) or {})
            changes: dict[str, Any] = {}
            if entry is None:
                channels.pop(channel_id, None)
                all_secrets.pop(channel_id, None)
                changes["removed"] = True
            else:
                channels[channel_id] = entry
                for key in set((entry or {}).keys()) | set((old_entry or {}).keys()):
                    if (old_entry or {}).get(key) != entry.get(key):
                        changes[key] = {"old": (old_entry or {}).get(key), "new": entry.get(key)}
                for name, value in (secrets or {}).items():
                    old = current.get(name)
                    if value is None:
                        current.pop(name, None)
                    else:
                        current[name] = value
                    if old != value:
                        changes[f"secret:{name}"] = {"old": _fingerprint(old), "new": _fingerprint(value)}
                all_secrets[channel_id] = current
            try:
                self._write(self.path, {**doc, "channels": channels, "updated_at": int(time.time())})
                self._write(self.secrets_path, {**sdoc, "channels": all_secrets}, secret=True)
                self._log(actor, action, channel_id, changes)
            except Exception:
                for path, data in zip((self.path, self.secrets_path), raw):
                    self._restore(path, data)
                raise

    def set_proactive_channel(self, value: str, actor: str) -> None:
        with self._lock:
            doc = self.doc()
            old = doc.get("proactive_channel", "auto")
            self._write(self.path, {**doc, "proactive_channel": value, "updated_at": int(time.time())})
            self._log(actor, "proactive_channel", value, {"old": old, "new": value})

    @staticmethod
    def _raw(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _restore(path: Path, raw: bytes | None) -> None:
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(raw)


class ChannelManager:
    """The configured channels of this Violet: adapters, their lifecycle and the sink they deliver into."""

    def __init__(self, runtime, store: ChannelStore | None = None, data_dir: str | os.PathLike[str] | None = None):
        self.runtime = runtime
        self.data_dir = data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data"
        self.store = store or ChannelStore(self.data_dir)
        self._lock = threading.RLock()
        self._adapters: dict[str, ChannelAdapter] = {}
        self._factory_overrides: dict[str, Callable[..., ChannelAdapter]] = {}
        runtime.channels = self

    # -- configuration ----------------------------------------------------------------------------------------
    def entries(self) -> dict[str, dict]:
        stored = self.store.channels()
        wechat = dict(stored.get(WECHAT) or {})
        out = {WECHAT: {"type": "wechat_ilink", "label": wechat.get("label") or "微信",
                        "enabled": wechat.get("enabled", True) is not False, "accept": "first_peer", "config": {}}}
        for channel_id, entry in stored.items():
            if channel_id != WECHAT and isinstance(entry, dict):
                out[channel_id] = entry
        return out

    def entry(self, channel_id: str) -> dict | None:
        return self.entries().get(channel_id)

    def context_for(self, channel_id: str | None) -> ChannelContext:
        entry = self.entry(channel_id or WECHAT) or self.entry(WECHAT)
        kind = entry.get("type", "wechat_ilink")
        # the platform's name (not the admin's label for it): the prompt says which app this conversation happens in
        return ChannelContext(channel=channel_id or WECHAT, channel_type=kind, label=label_for(kind))

    def accept_policy(self, channel_id: str) -> str:
        entry = self.entry(channel_id) or {}
        return "first_peer" if entry.get("type") == "wechat_ilink" else (entry.get("accept") or "owner")

    def proactive_channel(self) -> str:
        return str(self.store.doc().get("proactive_channel") or "auto")

    def validate(self, kind: str, config: dict[str, Any] | None, secrets: dict[str, Any] | None,
                 existing_secrets: dict[str, str] | None = None) -> tuple[dict, dict]:
        types = channel_types()
        if kind not in types:
            raise ValueError("不支持这种渠道")
        spec = types[kind]
        problems, clean_config, clean_secrets = {}, dict(spec["defaults"]), {}
        for key, value in (config or {}).items():
            if key not in spec["config"]:
                problems[key] = "这一项不能设置"
                continue
            try:
                clean_config[key] = spec["config"][key](value)
            except ValueError as exc:
                problems[key] = str(exc)
        for name, value in (secrets or {}).items():
            if name not in spec["secrets"]:
                problems[f"secret:{name}"] = "没有这个凭据"
                continue
            if value in (None, ""):
                continue                                     # empty = keep the stored value
            try:
                clean_secrets[name] = spec["secrets"][name][1](value)
            except ValueError as exc:
                problems[f"secret:{name}"] = str(exc)
        for key in spec.get("required_config") or []:
            if key not in problems and not clean_config.get(key):
                problems[key] = "必须填写"
        have = {**(existing_secrets or {}), **clean_secrets}
        for name, (required, _check) in spec["secrets"].items():
            if f"secret:{name}" in problems:
                continue                                     # keep the format message ("必须填写" would hide it)
            if required and not have.get(name):
                if kind == "onebot11" and name == "webhook_secret":
                    clean_secrets[name] = _secrets.token_urlsafe(24)     # generated; shown once in the response
                else:
                    problems[f"secret:{name}"] = "必须填写"
        if kind == "onebot11" and _host_kind(clean_config.get("api_base", "")) != "loopback" \
                and not have.get("access_token"):
            problems["secret:access_token"] = "接口不在本机时必须设置访问令牌"
        if problems:
            raise ValidationProblems(problems)
        return clean_config, clean_secrets

    # -- admin actions ------------------------------------------------------------------------------------------
    def add(self, kind: str, channel_id: str, label: str = "", config: dict | None = None, secrets: dict | None = None,
            enabled: bool = True, actor: str = "admin") -> dict[str, Any]:
        if not ID_RE.match(channel_id or "") or channel_id == WECHAT:
            raise ValidationProblems({"id": "标识只能用小写字母、数字、- 和 _，以字母开头（wechat 已被微信占用）"})
        if channel_id in self.entries():
            raise ValidationProblems({"id": "已经有这个标识的渠道了"})
        clean_config, clean_secrets = self.validate(kind, config, secrets)
        entry = {"type": kind, "label": (label or "").strip()[:30] or channel_types()[kind]["label"],
                 "enabled": bool(enabled), "accept": "owner", "config": clean_config, "created_at": int(time.time())}
        self.store.save(channel_id, entry, clean_secrets, actor, "add")
        self.apply(channel_id)
        shown = {"webhook_secret": clean_secrets["webhook_secret"]} if "webhook_secret" in clean_secrets else {}
        return {**self.describe_one(channel_id), "shown_once": shown}

    def update(self, channel_id: str, config: dict | None = None, secrets: dict | None = None,
               label: str | None = None, enabled: bool | None = None, actor: str = "admin") -> dict[str, Any]:
        entry = self.entry(channel_id)
        if entry is None:
            raise LookupError("没有这个渠道")
        new = dict(self.store.channels().get(channel_id) or {})
        if channel_id == WECHAT:
            if config or secrets:
                raise ValidationProblems({"config": "微信渠道只能开关，登录在“微信登录”页扫码"})
            new.update(type="wechat_ilink")
            clean_secrets = {}
        else:
            merged = {**(entry.get("config") or {}), **(config or {})}
            clean_config, clean_secrets = self.validate(entry["type"], merged, secrets, self.store.secrets(channel_id))
            new.update(entry, config=clean_config)
        if label is not None:
            new["label"] = label.strip()[:30] or new.get("label")
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise ValidationProblems({"enabled": "要用 true / false"})
            new["enabled"] = enabled
        self.store.save(channel_id, new, clean_secrets, actor, "update")
        self.apply(channel_id)
        return self.describe_one(channel_id)

    def remove(self, channel_id: str, actor: str = "admin") -> None:
        if channel_id == WECHAT:
            raise ValidationProblems({"id": "微信渠道不能删除，只能关闭"})
        if self.entry(channel_id) is None:
            raise LookupError("没有这个渠道")
        self.stop(channel_id)
        with self._lock:
            self._adapters.pop(channel_id, None)
        self.store.save(channel_id, None, None, actor, "remove")

    def regenerate_secret(self, channel_id: str, name: str, actor: str = "admin") -> dict[str, str]:
        entry = self.entry(channel_id)
        if entry is None or entry.get("type") != "onebot11" or name != "webhook_secret":
            raise ValidationProblems({name: "这个凭据不能自动生成"})
        value = _secrets.token_urlsafe(24)
        self.store.save(channel_id, dict(self.store.channels()[channel_id]), {name: value}, actor, "regenerate")
        self.apply(channel_id)
        return {"webhook_secret": value}

    # -- lifecycle ------------------------------------------------------------------------------------------------
    def _build(self, channel_id: str, entry: dict) -> ChannelAdapter:
        kind = entry.get("type")
        if kind == "wechat_ilink":
            from ai.channels.wechat_ilink import WeChatChannel
            return WeChatChannel(self.runtime, channel_id)
        factory = self._factory_overrides.get(kind) or channel_types()[kind]["class"]
        adapter = factory(channel_id, entry.get("config") or {}, self.store.secrets(channel_id), data_dir=self.data_dir)
        adapter.label = entry.get("label") or adapter.label
        return adapter

    def get(self, channel_id: str) -> ChannelAdapter | None:
        with self._lock:
            adapter = self._adapters.get(channel_id)
            if adapter is None:
                entry = self.entry(channel_id)
                if entry is None:
                    return None
                try:
                    adapter = self._build(channel_id, entry)
                except Exception:
                    logger.exception("channel %s could not be created", channel_id)
                    return None
                self._adapters[channel_id] = adapter
            return adapter

    def apply(self, channel_id: str) -> None:
        """Rebuild the adapter from the stored settings and start it if enabled (stop it if not)."""
        entry = self.entry(channel_id)
        with self._lock:
            old = self._adapters.pop(channel_id, None)
        if old is not None:
            try:
                old.stop()
            except Exception:
                logger.exception("stopping channel %s failed", channel_id)
        if entry is None:
            return
        if entry.get("enabled", True) and self.runtime_started():
            adapter = self.get(channel_id)
            if adapter is not None:
                adapter.start(self.runtime.ingest_message)

    def runtime_started(self) -> bool:
        return bool(getattr(self.runtime, "channels_active", False))

    def start_enabled(self) -> list[str]:
        self.runtime.channels_active = True
        started = []
        for channel_id, entry in self.entries().items():
            if not entry.get("enabled", True):
                continue
            adapter = self.get(channel_id)
            if adapter is None:
                continue
            try:
                adapter.start(self.runtime.ingest_message)
                started.append(channel_id)
            except Exception:
                logger.exception("channel %s did not start", channel_id)
        return started

    def stop(self, channel_id: str) -> None:
        with self._lock:
            adapter = self._adapters.get(channel_id)
        if adapter is not None:
            try:
                adapter.stop()
            except Exception:
                logger.exception("stopping channel %s failed", channel_id)

    def stop_all(self) -> None:
        self.runtime.channels_active = False
        for channel_id in list(self._adapters):
            self.stop(channel_id)

    def test(self, channel_id: str) -> dict[str, Any]:
        adapter = self.get(channel_id)
        if adapter is None:
            raise LookupError("没有这个渠道")
        probe = getattr(adapter, "probe", None)
        if probe is None:
            status = adapter.status()
            return {"ok": status.state == "connected", "state": status.state, "detail": status.detail}
        from ai.channels.normalize import safe_error
        try:
            return probe()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": safe_error(exc)}

    # -- reading ------------------------------------------------------------------------------------------------
    def describe_one(self, channel_id: str) -> dict[str, Any]:
        entry = self.entry(channel_id)
        if entry is None:
            raise LookupError("没有这个渠道")
        adapter = self._adapters.get(channel_id)
        status = adapter.status().to_dict() if adapter is not None else {"state": "stopped" if entry.get("enabled", True)
                                                                         else "disabled", "detail": ""}
        if not entry.get("enabled", True):
            status["state"], status["detail"] = "disabled", "已关闭"
        types = channel_types()
        secret_names = list((types.get(entry.get("type")) or {}).get("secrets") or {})
        stored = self.store.secrets(channel_id)
        from ai.channels.identity import IdentityStore
        identities = IdentityStore(self.runtime.db).list(channel_id) if channel_id != WECHAT else []
        return {"id": channel_id, "type": entry.get("type"), "label": entry.get("label"),
                "enabled": entry.get("enabled", True) is not False, "accept": self.accept_policy(channel_id),
                "config": dict(entry.get("config") or {}), "status": status,
                "secrets": [{"name": n, "is_set": bool(stored.get(n)), "fingerprint": _fingerprint(stored.get(n))}
                            for n in secret_names],
                "webhook_path": f"/channels/hook/{channel_id}" if entry.get("type") == "onebot11" else None,
                "peers": {s: sum(1 for i in identities if i["status"] == s) for s in ("bound", "pending", "blocked")}}

    def describe(self) -> list[dict[str, Any]]:
        return [self.describe_one(channel_id) for channel_id in self.entries()]


class ValidationProblems(ValueError):
    def __init__(self, problems: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems
