"""
BaseAgent: 所有 agent 的基类，提供通用的工具注册、MCP 管理和 LLM 调用能力。
"""

import asyncio
import contextvars
import json
import logging
import os
import threading
import time
from typing import Any, Callable

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    OpenAIError,
    RateLimitError,
    Timeout,
)

from ai.chat import LLMConfig, violetdb
from ai.mcp import MCPToolBridge, UNTRUSTED_TOOL_NOTICE, normalize_tool_result, server_applies_to_agent
from ai.debug_visualizer import visualize_agent_output, visualize_error
from ai.settings import ENV_PATH as _ENV_PATH, env_float, env_int

logger = logging.getLogger("violet.agent")

# Per-call context (performer_result, recalled_memories, ...). It used to be an
# instance attribute, so two threads running the same agent (chat turn + proactive
# run) could read each other's context. A ContextVar is per thread / per task.
_CALL_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "violet_agent_call_context", default=None
)


class LLMNotConfigured(RuntimeError):
    """Phase 10: a model call before the API key was set (the first-run wizard has not been completed)."""


def _local_address(url: str | None) -> bool:
    """A loopback / private-network host (or localhost): reached directly, never through a proxy."""
    import ipaddress
    from urllib.parse import urlsplit
    host = (urlsplit(url or "").hostname or "").lower()
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _record_tool_call(tool_name: str, result_text: str, agent: str) -> None:
    """Phase 10.6: one MCP tool call for the diagnosis (the error code only, never the result)."""
    from ai.diagnostics import record
    code = None
    if isinstance(result_text, str) and result_text.lstrip().startswith("{"):
        try:
            data = json.loads(result_text)
            code = data.get("error_code") if isinstance(data, dict) else None
        except ValueError:
            code = None
    server, _, tool = tool_name.partition("__")
    record("tool_call", code is None, server=server, tool=tool, code=code, agent=agent)


class BaseAgent:
    """
    所有 agent 的基类。

    子类应当重写：
        - system_prompt_env_key: 从 .env 读取提示词的环境变量名
        - default_system_prompt: 提示词默认值
        - build_context_messages(user_message): 构建发给 LLM 的消息列表
        - mcp_agent_name: 用于 MCP 服务器按 agent 授权（MCP_SERVERS[].agents / MCP_DEFAULT_AGENTS）

    工具注册：
        - register_tool(schema, handler): 注册本地函数工具
        - MCP 由 .env 中 MCP_SERVERS 或子类指定的 mcp_servers 管理
          MCP 连接在每次 run_async() 内按需建立并关闭；单个服务器失败不会让 agent 失败。

    配置重载：
        - reload(): 重新加载 .env
    """

    # 子类应重写以下三组 env key / default
    model_env_key: str = ""
    default_model: str = "gpt-4o"
    system_prompt_env_key: str = ""
    # the agent's real prompt, shipped with the program (relative to the project root); used when the environment does
    # not set system_prompt_env_key -- a fresh install has no prompt in its .env (they used to live only in env_example)
    default_prompt_file: str = ""
    default_system_prompt: str = "You are Violet, a helpful AI assistant."
    temperature_env_key: str = ""
    default_temperature: float = 0.2
    mcp_agent_name: str = ""
    answer_after_max_rounds: bool = False      # 2026-10-30: ChatAgent answers without tools instead of failing
    retryable_errors = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)

    # Test hook: callable(LLMConfig) -> client exposing .chat.completions.create(**kw).
    client_factory: Callable[[LLMConfig], Any] | None = None

    def __init__(self):
        self._tools: list[dict[str, Any]] = []           # 本地工具 schema 列表
        self._handlers: dict[str, Callable] = {}         # 本地工具处理函数
        self._state_lock = threading.RLock()
        self._config: LLMConfig | None = None
        self._client: Any = None
        self._client_config_id: int | None = None

        # 初始化时只加载配置和本地工具。
        # MCP 连接不能跨 asyncio.run() 持久化；会在每次 run_async() 内临时建立并关闭。
        self._config = self._load_config()
        self.register_tools()

    # ------------------------------------------------------------------
    # 调用上下文（每个线程/任务独立）
    # ------------------------------------------------------------------

    @property
    def _call_context(self) -> dict[str, Any]:
        value = _CALL_CONTEXT.get()
        if value is None:
            # run_async() used directly (without run()): keep one dict for this context so per-run
            # values (user message, tool guard) are not silently dropped
            value = {}
            _CALL_CONTEXT.set(value)
        return value

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    def _load_config(self) -> LLMConfig:
        """重新读取 .env 并返回 LLMConfig。Phase 9.0: the Control Center's saved settings go back on top of .env."""
        from ai.control_config import reload_env
        reload_env()
        cfg = LLMConfig()
        cfg.reply_model = self._load_model()
        cfg.reply_prompt = self._load_system_prompt()
        cfg.reply_temperature = self._load_temperature()
        cfg.fallback_model = self._load_fallback_model()
        # phase 10: the outbound guard refuses any message that repeats these instructions
        from ai.channels.outbound import register_instructions
        register_instructions(self.__class__.__name__, cfg.reply_prompt)
        return cfg

    def _load_model(self) -> str:
        """从环境变量读取此 agent 的模型名，找不到则使用默认值。"""
        raw = os.getenv(self.model_env_key, "") if self.model_env_key else ""
        return raw.strip() or self.default_model

    def _load_fallback_model(self) -> str:
        """可选备用模型：CHAT_LLM_MODEL -> CHAT_LLM_FALLBACK_MODEL。"""
        if not self.model_env_key.endswith("_LLM_MODEL"):
            return ""
        key = self.model_env_key[: -len("_LLM_MODEL")] + "_LLM_FALLBACK_MODEL"
        return os.getenv(key, "").strip()

    def _load_system_prompt(self) -> str:
        return self.configured_prompt()

    @classmethod
    def configured_prompt(cls) -> str:
        """此 agent 的提示词：环境变量（或后台选的版本）> 程序自带的提示词文件 > 一句话的内置默认值。"""
        raw = os.getenv(cls.system_prompt_env_key, "") if cls.system_prompt_env_key else ""
        if raw.strip():
            return raw.strip()
        if cls.default_prompt_file:
            from ai.settings import PROJECT_ROOT
            try:
                text = (PROJECT_ROOT / cls.default_prompt_file).read_text(encoding="utf-8").strip()
            except OSError:
                logger.error("prompt file %s is missing; %s runs with its one-line default", cls.default_prompt_file,
                             cls.__name__)
                text = ""
            if text:
                return text
        return cls.default_system_prompt

    def _load_temperature(self) -> float:
        """从环境变量读取此 agent 的温度，找不到则使用默认值。"""
        raw = os.getenv(self.temperature_env_key, "") if self.temperature_env_key else ""
        try:
            return float(raw) if raw else self.default_temperature
        except ValueError:
            return self.default_temperature

    # ------------------------------------------------------------------
    # 工具注册（本地函数工具）
    # ------------------------------------------------------------------

    def register_tool(self, schema: dict[str, Any], handler: Callable) -> None:
        """
        注册一个本地函数工具。

        Args:
            schema: OpenAI function tool schema，格式为
                    {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
            handler: 调用该工具时执行的函数，可以是同步或异步函数。
        """
        name = schema["function"]["name"]
        self._tools.append(schema)
        self._handlers[name] = handler

    def register_tools(self) -> None:
        """
        子类重写此方法以注册自己的工具。
        在 __init__ 和 reload() 时都会被调用。
        """
        pass

    # ------------------------------------------------------------------
    # 配置重载（/reload-env 触发）
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """重新加载 .env 并重置本地工具列表。正在运行的调用继续使用旧快照。"""
        with self._state_lock:
            self._tools = []
            self._handlers = {}
            self.register_tools()
            self._config = self._load_config()
            self._client = None
            self._client_config_id = None

    # ------------------------------------------------------------------
    # 上下文构建（子类应重写）
    # ------------------------------------------------------------------

    def build_context_messages(self, user_message: str) -> list[dict[str, Any]]:
        """
        构建发给 LLM 的消息列表。

        子类应重写此方法以添加个性化上下文（记忆、任务状态等）。
        默认实现仅包含 system prompt 和用户消息。
        """
        return [
            {"role": "system", "content": self._config.reply_prompt},
            {"role": "user", "content": user_message},
        ]

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    def _get_client(self, cfg: LLMConfig):
        factory = type(self).client_factory or BaseAgent.client_factory
        with self._state_lock:
            if self._client is not None and self._client_config_id == id(cfg):
                return self._client
            if factory is not None:
                client = factory(cfg)
            else:
                if not cfg.api_key:
                    # phase 10: Violet runs before the model service is set up; say what is missing, not an SDK error
                    raise LLMNotConfigured("还没有设置模型服务的 API Key（管理后台 → 初始化向导 / 设置 → 接口）")
                # SDK retries are disabled: retrying happens once, in
                # _create_completion_with_retry, under a total deadline.
                extra = {}
                if _local_address(cfg.api_url):
                    # phase 10.6: a model service on this machine / the LAN is never sent through HTTP_PROXY (a
                    # system proxy answered 502 for 127.0.0.1 in the 10.3 acceptance run)
                    from openai import DefaultHttpxClient
                    extra["http_client"] = DefaultHttpxClient(trust_env=False)
                client = OpenAI(
                    api_key=cfg.api_key,
                    base_url=cfg.api_url,
                    timeout=Timeout(
                        env_float("LLM_REQUEST_TIMEOUT_SECONDS", 90.0),
                        connect=env_float("LLM_CONNECT_TIMEOUT_SECONDS", 10.0),
                    ),
                    max_retries=0,
                    **extra,
                )
            self._client = client
            self._client_config_id = id(cfg)
            return client

    def _create_completion_with_retry(self, client, **kwargs):
        max_attempts = max(1, env_int("LLM_MAX_RETRY_ATTEMPTS", 3))
        base_delay = env_float("LLM_RETRY_BASE_DELAY", 3.0)
        deadline = time.monotonic() + env_float("LLM_TOTAL_TIMEOUT_SECONDS", 180.0)
        from ai.model_params import apply as apply_model_params
        for attempt in range(1, max_attempts + 1):
            try:
                return client.chat.completions.create(**apply_model_params(kwargs))
            except self.retryable_errors as exc:
                if attempt >= max_attempts:
                    raise
                retry_after = getattr(exc, "retry_after", None)
                response = getattr(exc, "response", None)
                if retry_after is None and response is not None:
                    retry_after = response.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after is not None else base_delay * attempt
                except (TypeError, ValueError):
                    delay = base_delay * attempt
                delay = max(0.0, min(delay, 60.0))
                if time.monotonic() + delay >= deadline:
                    logger.warning("[%s] LLM retry budget exhausted after %d attempt(s)", self.__class__.__name__, attempt)
                    raise
                logger.warning(
                    "[%s] retryable LLM error (%s), retry %d/%d in %.1fs",
                    self.__class__.__name__, exc.__class__.__name__, attempt, max_attempts - 1, delay,
                )
                time.sleep(delay)

    def _create_completion(self, client, cfg: LLMConfig, **kwargs):
        """Phase 10.6: every model call's outcome goes to the diagnosis (agent, model, error kind -- no content)."""
        from ai import trace
        from ai.diagnostics import record
        started = time.monotonic()
        offered = [t.get("function", {}).get("name") for t in kwargs.get("tools") or [] if isinstance(t, dict)]
        try:
            completion = self._create_completion_primary(client, cfg, **kwargs)
        except Exception as exc:
            from ai.setup_status import classify_error
            kind = classify_error(exc)[0]
            record("model", False, agent=self.__class__.__name__, model=kwargs.get("model"), error_kind=kind)
            trace.step("模型调用", agent=self.__class__.__name__, 模型=kwargs.get("model"), 给的工具=offered or None,
                       tool_choice=kwargs.get("tool_choice"), 耗时毫秒=int((time.monotonic() - started) * 1000),
                       结果=f"失败（{kind}：{exc.__class__.__name__}）")
            raise
        record("model", True, agent=self.__class__.__name__)
        from ai import usage as usage_ledger
        usage_ledger.record(self.__class__.__name__, getattr(completion, "model", None) or kwargs.get("model"),
                            getattr(completion, "usage", None))
        if trace.enabled():
            usage = getattr(completion, "usage", None)
            calls = []
            try:
                calls = [c.function.name for c in (completion.choices[0].message.tool_calls or [])]
            except (AttributeError, IndexError, TypeError):
                pass
            trace.step("模型调用", agent=self.__class__.__name__, 模型=getattr(completion, "model", None) or kwargs.get("model"),
                       给的工具=offered or None, tool_choice=kwargs.get("tool_choice"),
                       耗时毫秒=int((time.monotonic() - started) * 1000),
                       tokens=getattr(usage, "total_tokens", None), 要调用=calls or None, 结果="ok")
        return completion

    def _create_completion_primary(self, client, cfg: LLMConfig, **kwargs):
        """Primary model with bounded retries; one pass on the fallback model if configured."""
        try:
            return self._create_completion_with_retry(client, **kwargs)
        except (APIError, OpenAIError) as exc:
            fallback = getattr(cfg, "fallback_model", "")
            if not fallback or fallback == kwargs.get("model"):
                raise
            logger.warning(
                "[%s] model %s failed (%s); trying fallback model %s",
                self.__class__.__name__, kwargs.get("model"), exc.__class__.__name__, fallback,
            )
            return self._create_completion_with_retry(client, **{**kwargs, "model": fallback})

    def _mcp_servers_for_run(self, cfg: LLMConfig) -> list[dict[str, Any]]:
        # phase 9.2: the Control Center's tool settings are read at the start of every run (no reload needed)
        from ai.tool_manager import apply_tool_settings
        servers = apply_tool_settings(list(cfg.mcp_servers or []))
        return [s for s in servers if server_applies_to_agent(s, self.mcp_agent_name or None)]

    def _filter_tools_for_run(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Hook: tools offered in this run (subclasses may hide some, e.g. web tools in proactive mode)."""
        return tools

    def _new_tool_guard(self, user_message: str):
        """Hook: per-run guard with check(name, args) -> denial text | None and observe(name, result)."""
        return None

    def _tool_choice_for_round(self, round_index: int, tools: list[dict[str, Any]]):
        """Hook (phase 9.2): the tool_choice of one model call of this run -- "auto" unless an agent knows this turn
        must look something up first (ChatAgent on a tool turn: "required" for the first call)."""
        return "auto" if tools else None

    def _lookup_reminder(self, called: list[str]) -> str | None:
        """Hook (phase 9.2.2): when the model is about to answer, a system note asking it once more to look up what
        this turn still needs (None: answer as is). called: the tool names run so far in this run."""
        return None

    def _should_finish_after_tools(self, step: list[dict[str, Any]]) -> bool:
        """Hook: True ends the run right after this step's tools ran, without asking the model to sum up.

        step: one dict per tool call of the step -- name, result (text) and denied (bool). Only an agent
        whose final text nobody reads may return True (MemoryAgent background jobs); the default never does."""
        return False

    async def _call_local_tool(self, handler: Callable, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            result = handler(arguments)
            if asyncio.iscoroutine(result):
                result = await result
            return normalize_tool_result(result)
        except Exception as exc:
            logger.exception("[%s] local tool %s failed", self.__class__.__name__, tool_name)
            return json.dumps(
                {"ok": False, "error_code": "LOCAL_TOOL_ERROR", "tool": tool_name, "message": str(exc)[:300]},
                ensure_ascii=False,
            )

    async def run_async(
        self,
        user_message: str,
        max_tool_rounds: int = 5,
    ) -> dict[str, Any]:
        """
        异步调用 LLM，支持工具调用循环（含本地工具和 MCP 工具）。

        Returns:
            {"content": str, "messages": list, "raw": dict}
        """
        with self._state_lock:
            cfg = self._config
            local_tools = list(self._tools)
            handlers = dict(self._handlers)
        client = self._get_client(cfg)
        chat_history = self.build_context_messages(user_message)

        async with MCPToolBridge(self._mcp_servers_for_run(cfg), agent_name=self.mcp_agent_name or None) as mcp_bridge:
            try:                       # 2026-10-30: the filter may look at a tool's permission (read / write ...)
                self._call_context["_mcp_permissions"] = {n: b.permission for n, b in mcp_bridge.tool_to_session.items()}
            except Exception:          # noqa: BLE001 -- an agent without a call context
                pass
            all_tools = self._filter_tools_for_run(local_tools + list(mcp_bridge.tools))
            offered = {t["function"]["name"] for t in all_tools}
            guard = self._new_tool_guard(user_message)
            if any(name in mcp_bridge.tool_to_session for name in offered):
                insert_at = 1 if chat_history and chat_history[0].get("role") == "system" else 0
                chat_history.insert(insert_at, {"role": "system", "content": UNTRUSTED_TOOL_NOTICE})

            called: list[str] = []
            reminded = False
            for round_index in range(max_tool_rounds + 1):
                choice = self._tool_choice_for_round(round_index, all_tools)
                request = dict(model=cfg.reply_model, messages=chat_history, tools=all_tools or None,
                               tool_choice=choice, temperature=cfg.reply_temperature)
                try:
                    completion = self._create_completion(client, cfg, **request)
                except BadRequestError:
                    if choice in (None, "auto"):
                        raise
                    # a provider that does not accept a forced tool_choice: the turn still runs, the model decides
                    logger.warning("[%s] tool_choice=%r refused by the provider; retrying with auto",
                                   self.__class__.__name__, choice)
                    completion = self._create_completion(client, cfg, **{**request, "tool_choice": "auto"})
                assistant_message = completion.choices[0].message
                assistant_dict = assistant_message.model_dump(exclude_none=True)
                chat_history.append(assistant_dict)

                tool_calls = getattr(assistant_message, "tool_calls", None) or []
                if not tool_calls and not reminded and round_index < max_tool_rounds:
                    reminder = self._lookup_reminder(called)
                    if reminder:
                        # phase 9.2.2: one more round (tool_choice auto) instead of an answer missing a lookup
                        reminded = True
                        chat_history.append({"role": "system", "content": reminder})
                        continue
                if not tool_calls:
                    return {
                        "content": assistant_message.content or "",
                        "messages": chat_history,
                        "raw": completion.model_dump(),
                        "mcp_unavailable": list(mcp_bridge.unavailable),
                        "tools_called": list(called),
                        "web_finding_ids": list(getattr(guard, "finding_ids", []) or []),
                        **(guard.result_extras() if hasattr(guard, "result_extras") else {}),
                    }

                step: list[dict[str, Any]] = []
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        arguments = json.loads(tool_call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        logger.warning("[%s] tool %s called with invalid JSON arguments", self.__class__.__name__, tool_name)
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}

                    denial = guard.check(tool_name, arguments) if guard is not None else None
                    binding = mcp_bridge.tool_to_session.get(tool_name)
                    if denial is None and binding is not None and binding.extra.get("needs_approval"):
                        # 2026-10-30 (ai/permissions.py): this tool waits for the owner -- not run now
                        from ai.chat import violetdb as _db
                        from ai.permissions import ask_for_tool
                        denial = ask_for_tool(_db, tool_name, arguments)
                    refused = mcp_bridge.denied_tools + mcp_bridge.disabled_tools
                    if denial is None and tool_name not in offered and tool_name not in refused:
                        denial = json.dumps({"ok": False, "error_code": "UNKNOWN_TOOL",
                                             "message": f"Unknown tool: {tool_name}"}, ensure_ascii=False)
                    if denial is not None:
                        result_text = denial
                    elif tool_name in handlers:
                        result_text = await self._call_local_tool(handlers[tool_name], tool_name, arguments)
                    elif tool_name in mcp_bridge.tool_to_session or tool_name in refused:
                        result_text = await mcp_bridge.call_tool(tool_name, arguments)
                    else:
                        result_text = json.dumps(
                            {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"Unknown tool: {tool_name}"},
                            ensure_ascii=False,
                        )

                    if guard is not None and denial is None:
                        guard.observe(tool_name, result_text)
                    if "__" in tool_name and tool_name not in handlers:
                        _record_tool_call(tool_name, result_text, self.__class__.__name__)
                    from ai import trace
                    if trace.enabled():
                        outcome, why = trace.tool_outcome(result_text)
                        trace.step("工具调用", agent=self.__class__.__name__, 工具=tool_name, 参数=trace.text(arguments),
                                   结果=("被拦下：" + outcome) if denial is not None else outcome, 原因=why,
                                   长度=len(result_text or ""), 内容=trace.text(result_text))
                    chat_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    })
                    step.append({"name": tool_name, "result": result_text, "denied": denial is not None})
                    called.append(tool_name)

                if self._should_finish_after_tools(step):
                    return {
                        "content": "",
                        "messages": chat_history,
                        "raw": completion.model_dump(),
                        "mcp_unavailable": list(mcp_bridge.unavailable),
                        "finished_after_tools": True,
                    }

            if not self.answer_after_max_rounds:
                raise RuntimeError(f"{self.__class__.__name__}: max tool rounds exceeded")
            # 2026-10-30 (free tool use): out of tool rounds -- one last call without tools, so she answers from what
            # she has instead of the whole turn failing
            chat_history.append({"role": "system", "content": "工具已经用够了，这一轮不能再调用工具。按上面已经拿到的结果直接回复；"
                                                              "没拿到的照实说没拿到。"})
            completion = self._create_completion(client, cfg, model=cfg.reply_model, messages=chat_history,
                                                 temperature=cfg.reply_temperature)
            assistant_message = completion.choices[0].message
            chat_history.append(assistant_message.model_dump(exclude_none=True))
            return {
                "content": assistant_message.content or "",
                "messages": chat_history,
                "raw": completion.model_dump(),
                "mcp_unavailable": list(mcp_bridge.unavailable),
                "tools_called": list(called),
                "max_rounds_reached": True,
                "web_finding_ids": list(getattr(guard, "finding_ids", []) or []),
                **(guard.result_extras() if hasattr(guard, "result_extras") else {}),
            }

    def run(self, user_message: str, call_context: dict[str, Any] | None = None, **kwargs) -> dict[str, Any]:
        """同步调用 LLM（run_async 的同步包装）。

        Args:
            call_context: 额外数据（如 performer_result），由 pipeline 注入，
                          build_context_messages 可通过 self._call_context 读取。
                          只对本线程/本次调用可见，调用结束后自动清空。
        """
        token = _CALL_CONTEXT.set(dict(call_context or {}))
        try:
            result = asyncio.run(self.run_async(user_message, **kwargs))
            # Visualize output
            extra_info = {
                "user_message": user_message,
                "call_context": call_context,
            } if call_context else {"user_message": user_message}
            visualize_agent_output(
                agent_name=self.__class__.__name__,
                output=result,
                extra_info=extra_info,
            )
            return result
        except Exception as e:
            visualize_error(
                agent_name=self.__class__.__name__,
                error=e,
                context={"user_message": user_message, "call_context": call_context},
            )
            raise
        finally:
            _CALL_CONTEXT.reset(token)
