"""First-run setup (phase 10.3): what still has to be configured, and a real check of the model service.

The Control Center's "开始" page reads ``checklist()``: one item per area (admin password, model service, agent
models, chat channels, persona and profile, tools, proactive messages / virtual life), each with a state

  ok        configured and checked
  todo      required, not done yet (Violet cannot chat until it is)
  warn      configured but the last check failed, or a default that probably does not exist on this API
  optional  not needed to start

and the tab where it is fixed. Nothing here writes settings: the wizard saves through the existing settings / channel
APIs, so validation, history and rollback stay in one place.

``test_model`` makes ONE short request with the settings Violet is using now (the Chat agent's client) and keeps only the
outcome (ok, model, latency, error kind) in <data_dir>/control/setup.json -- never the key, the request or the answer.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("violet.setup")

REQUIRED = ("admin", "model_service", "agent_models", "channels")


def _state_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    return Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data") / "control" / "setup.json"


def load_state(data_dir=None) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(update: dict[str, Any], data_dir=None) -> dict[str, Any]:
    path = _state_path(data_dir)
    data = {**load_state(data_dir), **update}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return data


def _settings_fingerprint() -> str:
    """Which API address / key / chat model a model test result belongs to (a hash: a later change makes the old
    result stale instead of showing a green tick for settings that were never tested)."""
    import hashlib
    raw = "|".join(os.environ.get(k, "").strip() for k in ("LLM_API_URL", "LLM_API_KEY", "CHAT_LLM_MODEL"))
    return hashlib.sha256(("violet-setup:" + raw).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ model test

def classify_error(exc: BaseException) -> tuple[str, str]:
    """(kind, plain words for the admin). Never the exception text (it can quote the address or a header)."""
    name = exc.__class__.__name__
    status = getattr(exc, "status_code", None)
    if name == "LLMNotConfigured":
        return "not_configured", "还没有填写 API 地址或 API Key"
    if name == "AuthenticationError" or status == 401:
        return "auth", "API Key 不对或已失效（接口返回 401）"
    if name == "PermissionDeniedError" or status == 403:
        return "forbidden", "这个 Key 没有权限使用该模型（接口返回 403）"
    if name == "NotFoundError" or status == 404:
        return "not_found", "接口地址不对，或者模型名在这个服务上不存在（接口返回 404）"
    if name == "RateLimitError" or status == 429:
        return "rate_limited", "请求太频繁或额度用完了（接口返回 429）"
    if name == "BadRequestError" or status == 400:
        return "bad_request", "接口拒绝了请求（400）：多半是模型名不对或参数不被支持"
    if name in ("APITimeoutError", "Timeout", "TimeoutError", "ReadTimeout"):
        return "timeout", "等待接口回应超时"
    if name in ("APIConnectionError", "ConnectionError", "ConnectError"):
        return "connection", "连不上 API 地址（检查地址、网络或代理）"
    if isinstance(status, int) and status >= 500:
        return "server", f"接口服务出错（{status}），稍后再试"
    return "error", f"调用失败（{name}）"


def test_model(chat_agent, data_dir=None, model: str | None = None) -> dict[str, Any]:
    """One short request through the Chat agent's own client (the address, key, timeouts and model parameters Violet
    really uses). Returns and stores {ok, model, latency_ms, kind, message, at}."""
    started = time.monotonic()
    cfg = getattr(chat_agent, "_config", None)
    chosen = (model or getattr(cfg, "reply_model", "") or "").strip()
    result: dict[str, Any] = {"model": chosen, "at": int(time.time())}
    try:
        if cfg is None:
            raise RuntimeError("chat agent has no configuration")
        client = chat_agent._get_client(cfg)
        from ai.model_params import apply as apply_model_params
        response = client.chat.completions.create(**apply_model_params({
            "model": chosen, "messages": [{"role": "user", "content": "请只回复两个字：在的"}], "max_tokens": 16,
            "temperature": 0}))
        content = ""
        try:
            content = (response.choices[0].message.content or "").strip()
        except (AttributeError, IndexError, TypeError):
            pass
        result.update(ok=True, kind="ok", message="模型服务可以正常使用" + ("" if content else "（回复是空的，但调用成功了）"))
    except Exception as exc:  # noqa: BLE001 -- every failure becomes a plain-words result
        kind, message = classify_error(exc)
        result.update(ok=False, kind=kind, message=message)
        logger.warning("model test failed: %s (%s)", kind, exc.__class__.__name__)
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    result["settings"] = _settings_fingerprint()
    save_state({"model_test": result}, data_dir)
    return result


def list_models(chat_agent) -> dict[str, Any]:
    """The model names the API offers (OpenAI-compatible GET /models), to pick from instead of typing them."""
    cfg = getattr(chat_agent, "_config", None)
    try:
        client = chat_agent._get_client(cfg)
        page = client.models.list()
        names = sorted({getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
                        for m in getattr(page, "data", page) or []} - {None})
        return {"ok": True, "models": names[:500]}
    except Exception as exc:  # noqa: BLE001
        kind, message = classify_error(exc)
        if isinstance(exc, AttributeError):
            kind, message = "unsupported", "这个接口不支持列出模型，请直接填写模型名"
        elif kind == "not_found":       # no model is involved here: a 404 is the address (or no /models on it)
            message = "接口返回 404：多半是 API 地址不对（一般以 /v1 结尾）；地址没错的话，就是这个服务不支持列出模型"
        return {"ok": False, "kind": kind, "message": message, "models": []}


# ------------------------------------------------------------------------------------------------ checklist

def _item(item_id: str, title: str, state: str, detail: str, tab: str, required: bool,
          anchor: str | None = None) -> dict[str, Any]:
    """tab + anchor: where clicking the item on the start page goes (the page, then the element with that id)."""
    return {"id": item_id, "title": title, "state": state, "detail": detail, "tab": tab, "anchor": anchor,
            "required": required}


def checklist(*, admin_store=None, agents=None, runtime=None, data_dir=None, violetdb=None) -> dict[str, Any]:
    items = []
    # 1. administrator
    if admin_store is not None:
        data = admin_store.load()
        if not data.get("password_hash"):
            items.append(_item("admin", "管理员密码", "todo", "还没有设置密码（现在用的是管理令牌）", "account", True, "acct-pw"))
        elif data.get("must_change"):
            # Violet stage 3: the installer's random password is fine to keep
            items.append(_item("admin", "管理员密码", "ok", "安装时随机生成的密码（可以一直用）", "account", True, "acct-pw"))
        else:
            items.append(_item("admin", "管理员密码", "ok", "已设置", "account", True, "acct-pw"))
    # 2. model service
    from ai.chat import llm_configured
    state = load_state(data_dir)
    test = state.get("model_test") or {}
    if not llm_configured():
        items.append(_item("model_service", "模型服务", "todo", "填写 API 地址和 API Key", "model", True, "model-api"))
    elif not test or test.get("settings") != _settings_fingerprint():
        items.append(_item("model_service", "模型服务", "warn", "已填写，还没有测试过对话（选好模型后点“保存模型并测试对话”）",
                           "model", True, "model-agents"))
    elif test.get("ok"):
        items.append(_item("model_service", "模型服务", "ok",
                           f"测试通过（{test.get('model')}，{test.get('latency_ms')} 毫秒）", "model", True, "model-api"))
    else:
        items.append(_item("model_service", "模型服务", "warn", f"上次测试失败：{test.get('message')}", "model", True, "model-api"))
    # 3. agent models
    defaults, names = [], []
    for agent in agents or []:
        cfg, key = getattr(agent, "_config", None), str(getattr(agent, "model_env_key", "") or "")
        if cfg is None or not key:
            continue
        names.append(f"{agent.__class__.__name__.replace('Agent', '')}: {cfg.reply_model}")
        if not os.environ.get(key, "").strip():
            defaults.append(agent.__class__.__name__.replace("Agent", ""))
    if defaults:
        items.append(_item("agent_models", "各 Agent 的模型", "warn",
                           "这些 Agent 还在用代码默认模型（这个服务上可能没有）：" + "、".join(defaults), "model", True, "model-agents"))
    else:
        items.append(_item("agent_models", "各 Agent 的模型", "ok", "；".join(names) or "—", "model", True, "model-agents"))
    # 4. chat channels
    manager = getattr(runtime, "channels", None) if runtime is not None else None
    if manager is not None:
        rows = manager.describe()
        live = [c for c in rows if c["status"].get("state") == "connected"]
        bound = sum(c["peers"]["bound"] for c in rows) + sum(1 for c in live if c["type"] == "wechat_ilink")
        if live and bound:
            items.append(_item("channels", "聊天渠道", "ok", "已连接：" + "、".join(c["label"] for c in live), "channels", True, "ch-list"))
        elif live:
            # the channel's own card shows "最后一步：绑定你的账号" with the binding code button
            items.append(_item("channels", "聊天渠道", "todo", "渠道已连接，但还没有绑定你的账号（生成绑定码）",
                               "channels", True, "ch-card-" + live[0]["id"]))
        elif connecting := [c["label"] for c in rows if c["status"].get("state") in ("starting", "reconnecting")]:
            # 0.7.6: just started / reconnecting -- not "nothing set up"; the page looks again in a few seconds
            items.append({**_item("channels", "聊天渠道", "warn", "正在连接：" + "、".join(connecting) + "（几秒后自动刷新）",
                                  "channels", True, "ch-list"), "connecting": True})
        else:
            needs = [c["label"] for c in rows if c["status"].get("state") == "needs_login"]
            detail = ("扫码登录：" + "、".join(needs)) if needs else "还没有可用的聊天渠道（微信、QQ、Telegram 连上任意一个就行）"
            items.append(_item("channels", "聊天渠道", "todo", detail, "channels", True, "ch-list"))
    # 5. persona and profile (the shipped persona works as is)
    items.append(_item("persona", "人设与用户档案", "optional",
                       "已使用默认人设；可以在“人设与生活”页查看，用户档案会在聊天中逐步建立", "persona", False))
    # 6. tools
    try:
        from ai.chat import get_mcp_servers
        servers = [s for s in get_mcp_servers() if isinstance(s, dict)]
    except Exception:
        servers = []
    items.append(_item("tools", "工具（MCP）", "optional",
                       f"已配置 {len(servers)} 个工具服务器" if servers else "没有配置外部工具；不影响聊天", "tools", False, "phone-mcp"))
    # 7. proactive / life
    from ai.settings import env_bool
    items.append(_item("features", "主动消息与虚拟生活", "optional",
                       ("生活引擎开" if env_bool("VIOLET_LIFE_ENGINE", False) else "生活引擎关") + "；主动消息在“设置”里调整",
                       "settings", False, "set-g-proactive"))
    required = [i for i in items if i["required"]]
    complete = all(i["state"] == "ok" for i in required)
    return {"complete": complete, "items": items, "model_test": test or None,
            "next": next((i for i in required if i["state"] != "ok"), None)}
