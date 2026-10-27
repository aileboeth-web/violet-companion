"""Model compatibility check for OpenAI-compatible Chat Completions endpoints (phase 7 B.1).

    python -m tools.model_compat                                    # mock providers, offline
    python -m tools.model_compat --real --api-url URL --model M1 --model M2 [--write-policy]

Each check is one small request (max 16-64 output tokens) and is judged only by
what the endpoint actually answers -- never by the model's name:

    basic                 plain text answer
    temperature           temperature=0.2 accepted
    max_tokens            "max_tokens" accepted
    max_completion_tokens "max_completion_tokens" accepted
    reasoning_effort      "reasoning_effort" accepted (the legacy vision call sends it)
    tools                 a tool call comes back for a request that needs one
    tool_roundtrip        a tool result is accepted and answered (multi-round tool use)
    json_object           response_format json_object -> valid JSON
    json_prompt           JSON by instruction only (how Violet's intent / memory / proactive agents work)
    error_unknown_model   an unknown model name gives a clean 4xx (no tokens used)
    timeout               client-side timeout handling (only with --include-timeout in real mode)

429 / 500 cannot be provoked on a real endpoint without load; their handling is
verified offline against the mock providers. The result suggests a
config/model_params.json entry; --write-policy writes it only after a real run
and a confirmation. No provider-side setting is ever changed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from tools.harness import Budget, IsolationError, RealApiGate, fit_prompt_tokens, mask, request_stats, usage_fields

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")

PROMPT_OK = "只回复一个字：好"
TOOL = {"type": "function", "function": {
    "name": "query_memory", "description": "查询用户以前说过的事情",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
MOCK_MODELS = ["mock-standard", "mock-reasoning", "mock-no-tools", "mock-flaky"]


class CompatClient:
    def __init__(self, base_url: str, key: str, transport: httpx.BaseTransport | None = None, max_requests: int = 12):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.transport = transport
        self.max_requests = max_requests
        self.requests = 0

    def post(self, body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        stats = request_stats(body)
        if self.requests >= self.max_requests:
            return {"status": None, "error": "request cap reached", "skipped": True, "request": stats}
        self.requests += 1
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)), trust_env=False,
                              follow_redirects=False, transport=self.transport) as client:
                r = client.post(f"{self.base_url}/chat/completions", json=body,
                                headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"})
        except httpx.TimeoutException as exc:
            return {"status": None, "error": f"timeout ({exc.__class__.__name__})", "latency_ms": _ms(started),
                    "request": stats}
        except httpx.HTTPError as exc:
            return {"status": None, "error": f"transport ({exc.__class__.__name__})", "latency_ms": _ms(started),
                    "request": stats}
        try:
            payload = r.json()
        except ValueError:
            payload = None
        error = None
        if r.status_code != 200:
            err = (payload or {}).get("error") if isinstance(payload, dict) else None
            error = str((err or {}).get("message") if isinstance(err, dict) else (err or r.text[:200]))[:300]
            error = error.replace(self.key, "<redacted>") if self.key else error
        return {"status": r.status_code, "payload": payload if r.status_code == 200 else None, "error": error,
                "latency_ms": _ms(started), "request": stats}

    def post_classified(self, body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        result = self.post(body, timeout)
        kind = classify(result.get("status"), result.get("error"))
        result["kind"], result["retryable"] = kind, kind in RETRYABLE
        return result


def classify(status: int | None, error: str | None) -> str | None:
    """Same buckets as Violet's retry policy: rate_limited / server_error / timeout / transport are retried, 4xx not."""
    if status == 200:
        return None
    if status == 429:
        return "rate_limited"
    if status is not None and status >= 500:
        return "server_error"
    if status is not None:
        return "bad_request"
    if (error or "").startswith("timeout"):
        return "timeout"
    return "skipped" if error == "request cap reached" else "transport"


RETRYABLE = {"rate_limited", "server_error", "timeout", "transport"}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _message(result: dict[str, Any]) -> dict[str, Any]:
    try:
        return result["payload"]["choices"][0]["message"] or {}
    except (TypeError, KeyError, IndexError):
        return {}


def _usage(result: dict[str, Any]) -> dict[str, Any]:
    """Token fields as reported; None where the provider did not report them (never inferred)."""
    payload = result.get("payload")
    return usage_fields(payload.get("usage") if isinstance(payload, dict) else None)


def model_summary(checks: dict[str, Any]) -> dict[str, Any]:
    rows = [c for c in checks.values() if c.get("status") is not None]
    totals = {}
    for k in TOKEN_FIELDS:
        values = [c[k] for c in rows if c.get(k) is not None]
        totals[k] = {"sum": sum(values) if values else None, "calls_reporting": len(values)}
    fit_rows = [{"request_chars": c["request_chars"], "tools_chars": c["tools_chars"], "prompt_tokens": c["prompt_tokens"]}
                for c in rows if c.get("prompt_tokens") is not None]
    return {"answered_requests": len(rows),
            "usage_missing": sum(1 for c in rows if c.get("status") == 200 and not c.get("usage_present")),
            "tokens": totals, "fit_prompt_tokens_vs_request_chars": fit_prompt_tokens(fit_rows)}


def check_model(client: CompatClient, model: str, include_timeout: bool = False) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    base = {"model": model, "messages": [{"role": "user", "content": PROMPT_OK}]}

    def entry(result: dict[str, Any], ok: bool, error: str | None = None) -> dict[str, Any]:
        usage = _usage(result)
        request = result.get("request") or {"messages": 0, "request_chars": 0, "tools": 0, "tools_chars": 0}
        return {"ok": ok, "status": result.get("status"), "error": result.get("error") or error,
                "kind": result.get("kind"), "retryable": result.get("retryable"),
                "latency_ms": result.get("latency_ms"), "tokens": usage["total_tokens"], **usage, **request,
                "skipped": result.get("skipped", False)}

    def run(name: str, body: dict[str, Any], judge: Callable[[dict[str, Any]], bool], timeout: float = 30.0) -> bool:
        result = client.post_classified(body, timeout)
        ok = result.get("status") == 200 and judge(result)
        checks[name] = entry(result, ok, None if ok or result.get("error") else "unexpected answer")
        return ok

    has_text = lambda r: bool((_message(r).get("content") or "").strip())  # noqa: E731
    run("max_tokens", {**base, "max_tokens": 16}, has_text)
    run("max_completion_tokens", {**base, "max_completion_tokens": 16}, has_text)
    limit = {"max_tokens": 16} if checks["max_tokens"]["ok"] else (
        {"max_completion_tokens": 16} if checks["max_completion_tokens"]["ok"] else {})
    run("basic", {**base, **limit}, has_text)
    run("temperature", {**base, **limit, "temperature": 0.2}, has_text)
    run("reasoning_effort", {**base, **limit, "reasoning_effort": "low"}, has_text)
    tool_body = {"model": model, "messages": [{"role": "user", "content": "用 query_memory 查一下我上周说的考试"}],
                 "tools": [TOOL], "tool_choice": "auto", **{k: 64 for k in limit}}
    tool_result = client.post_classified(tool_body)
    calls = _message(tool_result).get("tool_calls") or []
    checks["tools"] = entry(tool_result, tool_result.get("status") == 200 and bool(calls),
                            None if calls else "no tool call returned")
    if calls:
        call = calls[0]
        follow = {**tool_body, "messages": tool_body["messages"] + [
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call.get("id", "call_1"), "content": json.dumps({"results": ["期中考试在周三"]},
                                                                                          ensure_ascii=False)}]}
        run("tool_roundtrip", follow, has_text)
    else:
        checks["tool_roundtrip"] = {"ok": False, "status": None, "error": "no tool call to answer", "skipped": True}

    def is_json(r):
        try:
            return isinstance(json.loads(_message(r).get("content") or ""), dict)
        except ValueError:
            return False
    json_messages = [{"role": "user", "content": '只输出 JSON：{"ok": true}'}]
    run("json_object", {"model": model, "messages": json_messages, "response_format": {"type": "json_object"},
                        **{k: 32 for k in limit}}, is_json)
    run("json_prompt", {"model": model, "messages": json_messages, **{k: 32 for k in limit}}, is_json)
    unknown = client.post_classified({"model": "violet-compat-no-such-model-0000", "messages": base["messages"], **limit})
    checks["error_unknown_model"] = entry(unknown, unknown.get("kind") == "bad_request")
    if include_timeout:
        result = client.post_classified({**base, **limit}, timeout=0.001)
        checks["timeout"] = entry(result, result.get("kind") == "timeout")
    return {"model": model, "checks": checks, "suggested_policy": suggest_policy(checks),
            "summary": model_summary(checks)}


def suggest_policy(checks: dict[str, Any]) -> dict[str, Any] | None:
    if not (checks.get("basic") or {}).get("ok"):
        return None                                    # nothing usable was observed: suggest nothing
    field = ("max_tokens" if checks["max_tokens"]["ok"] else
             "max_completion_tokens" if checks["max_completion_tokens"]["ok"] else "none")
    return {"send_temperature": bool(checks["temperature"]["ok"]), "max_tokens_field": field,
            "allowed_extra_params": ["reasoning_effort"] if checks["reasoning_effort"]["ok"] else [],
            "_tools": bool(checks["tools"]["ok"]), "_json_prompt": bool(checks["json_prompt"]["ok"])}


# ---------------------------------------------------------------------------
# Mock providers (offline)
# ---------------------------------------------------------------------------

def mock_transport() -> httpx.MockTransport:
    flaky_state = {"n": 0}

    def ok(content: str | None = None, tool_calls=None):
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return httpx.Response(200, json={"choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                                         "usage": {"total_tokens": 12}})

    def handler(request: httpx.Request) -> httpx.Response:
        if (request.extensions.get("timeout") or {}).get("read", 30) < 0.01:
            raise httpx.ReadTimeout("mock: read timeout", request=request)
        body = json.loads(request.content)
        model = body.get("model")
        if model not in MOCK_MODELS:
            return httpx.Response(404, json={"error": {"message": f"model {model} not found"}})
        if model == "mock-flaky":
            flaky_state["n"] += 1
            if flaky_state["n"] % 3 == 1:
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            if flaky_state["n"] % 3 == 2:
                return httpx.Response(500, text="upstream error")
            raise httpx.ReadTimeout("slow upstream", request=request)
        if model == "mock-reasoning":
            if "temperature" in body and body["temperature"] != 1:
                return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature'"}})
            if "max_tokens" in body:
                return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'"}})
        if body.get("tools") and model != "mock-no-tools":
            if any(m.get("role") == "tool" for m in body["messages"]):
                return ok("期中考试在周三。")
            return ok(None, [{"id": "call_1", "type": "function",
                              "function": {"name": "query_memory", "arguments": '{"query": "考试"}'}}])
        if "JSON" in body["messages"][-1]["content"]:
            return ok('{"ok": true}')
        return ok("好")
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def write_policy(results: list[dict[str, Any]], note: str, path: Path | None = None) -> Path:
    from ai.model_params import policy_path
    path = path or policy_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"defaults": {}, "models": {}}
    for result in results:
        if result["suggested_policy"]:
            entry = {k: v for k, v in result["suggested_policy"].items() if not k.startswith("_")}
            data.setdefault("models", {})[result["model"]] = {**entry, "_verified": note}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None, input_fn: Callable[[str], str] = input,
         transport: httpx.BaseTransport | None = None, environ: dict[str, str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.model_compat", description="模型兼容性检查（OpenAI 兼容接口）")
    p.add_argument("--real", action="store_true")
    p.add_argument("--api-url")
    p.add_argument("--model", action="append", default=[])
    p.add_argument("--include-timeout", action="store_true", help="真实模式下也测超时（会发出一个请求）")
    p.add_argument("--max-requests-per-model", type=int, default=11)
    p.add_argument("--write-policy", action="store_true", help="真实检查后，把建议写入 config/model_params.json（需确认）")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    environ = os.environ if environ is None else environ
    models = args.model or MOCK_MODELS
    try:
        import getpass
        gate = RealApiGate(environ, input_fn=input_fn, key_prompt=getpass.getpass)
        budget = Budget(max_calls_per_turn=args.max_requests_per_model,
                        max_calls_total=args.max_requests_per_model * len(models), max_tokens_total=20_000)
        key = gate.open(args.real, args.api_url, budget, {f"model{i + 1}": m for i, m in enumerate(models)},
                        assume_yes=args.yes)
    except PermissionError as exc:
        print(f"未启动：{exc}")
        return 2
    if key:
        base_url, tx = environ["LLM_API_URL"], transport
        environ["LLM_API_KEY"] = ""                       # not needed by anything else here
    else:
        if args.model and not set(args.model) <= set(MOCK_MODELS):
            print(f"模拟模式只认识 {MOCK_MODELS}；真实模型请加 --real")
            return 2
        base_url, key, tx = "https://mock.invalid/v1", "mock-key", transport or mock_transport()
    results = []
    for model in models:
        client = CompatClient(base_url, key, tx, max_requests=args.max_requests_per_model)
        result = check_model(client, model, include_timeout=args.include_timeout or not args.real)
        result["requests"] = client.requests
        results.append(result)
        oks = sum(1 for c in result["checks"].values() if c.get("ok"))
        kinds = sorted({c["kind"] for c in result["checks"].values() if c.get("kind") and not c.get("ok")})
        total = (((result.get("summary") or {}).get("tokens") or {}).get("total_tokens") or {}).get("sum")
        print(f"{model}: {oks}/{len(result['checks'])} 项通过，{client.requests} 个请求，"
              f"上游报告 total_tokens {total if total is not None else '无'}；错误类型 {kinds or '-'}；"
              f"建议策略 {result['suggested_policy']}")
    report = {"created_at": datetime.now().isoformat(timespec="seconds"), "real": bool(args.real),
              "endpoint": base_url, "key": mask(key) if args.real else None, "results": results}
    out_dir = args.out or Path(__file__).resolve().parent / "output" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "model_compat.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2).replace(key, "<redacted>"), encoding="utf-8")
    print(f"报告：{path}")
    if args.write_policy:
        if not args.real:
            print("模拟结果不能写入策略文件。")
            return 2
        if input_fn("把上面的建议写入 config/model_params.json？输入 yes: ").strip().lower() == "yes":
            target = write_policy(results, f"{datetime.now():%Y-%m-%d} tools.model_compat, endpoint {base_url}, report {path.name}")
            print(f"已写入 {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
