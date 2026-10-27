"""Chat with the real Violet 2.0 pipeline in an isolated sandbox (phase 7 B.1).

    python -m tools.violet_test_chat                     # mock model (default, free, offline)
    python -m tools.violet_test_chat --real --api-url https://relay.example/v1 [--config tools/violet_test.env]

Real calls need VIOLET_TEST_ALLOW_REAL_API=1 in the shell, --real, a test key
(VIOLET_TEST_API_KEY or the hidden prompt) and typing "yes". Use only fictional
information. Commands inside the chat:

    /advance <hours>   pretend time passed        /proactive   let Violet decide once whether to write
    /state             facts, memories, ...       /usage       calls, tokens, latency, errors
    /score <0-2> [note] score the last round      /quit        stop (also Ctrl+C)
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Callable

from tools.harness import (Budget, CallRecorder, IsolationError, RealApiGate, cleanup, created_this_run,
                           finish_session, list_leftovers, prepare_environment, seed_workdir, summarize_calls,
                           validate_image_config, write_mock_image_config, write_report)

MODEL_KEYS = {"intent": "INTENT_LLM_MODEL", "chat": "CHAT_LLM_MODEL", "memory": "MEMORY_LLM_MODEL",
              "proactive": "PROACTIVE_LLM_MODEL", "performer": "PERFORMER_LLM_MODEL"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.violet_test_chat", description="Violet 2.0 本地测试聊天（隔离环境）")
    p.add_argument("--real", action="store_true", help="调用真实模型（还需要 VIOLET_TEST_ALLOW_REAL_API=1 和测试 Key）")
    p.add_argument("--api-url", help="测试接口地址（OpenAI 兼容，例如 https://relay.example/v1）")
    p.add_argument("--config", type=Path, help="测试配置文件（只允许模型名、提示词、温度等，不允许写 Key）")
    p.add_argument("--max-calls-per-round", type=int, default=15)
    p.add_argument("--max-calls", type=int, default=120)
    p.add_argument("--max-tokens", type=int, default=300_000)
    p.add_argument("--price-per-1k", type=float, help="每 1K token 的价格（未知就不填，只按次数和 token 上限）")
    p.add_argument("--max-cost", type=float)
    p.add_argument("--quiet-hours", choices=("config", "off"), default="config", help="主动消息的静默时段")
    p.add_argument("--no-memory", action="store_true", help="模拟记忆模型什么都不保存")
    p.add_argument("--workdir", type=Path, help="测试数据目录（默认临时目录，结束后删除）")
    p.add_argument("--keep", action="store_true", help="结束后保留测试数据目录")
    p.add_argument("--yes", action="store_true", help="跳过 yes 确认（只在你已确认费用时使用）")
    p.add_argument("--verbose", action="store_true", help="显示 Violet 内部流水线输出")
    p.add_argument("--list-leftovers", action="store_true", help="只列出以前遗留的测试目录（不删除）然后退出")
    p.add_argument("--output-dir", type=Path, help="记录保存目录（默认 tools/output/<时间>）")
    p.add_argument("--image-mock", action="store_true",
                   help="打开图片功能，但只用离线 Mock 图片模型（不联网、不需要 Key）；图片只保存到测试目录")
    return p


def budget_from(args) -> Budget:
    return Budget(max_calls_per_turn=args.max_calls_per_round, max_calls_total=args.max_calls,
                  max_tokens_total=args.max_tokens, price_per_1k_tokens=args.price_per_1k, max_cost=args.max_cost)


def open_session(args, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
                 db=None, environ=None, db_factory: Callable[[Path], object] | None = None,
                 seed_from: Path | None = None):
    """Isolation first, then (maybe) the real-API gate, then the Violet session.

    db_factory(workdir) (tests) opens a database inside the test directory that the session owns and
    closes; without it the CLI uses the process-wide connection, which also points into that directory.
    seed_from: a kept test directory whose database and evaluation state are copied (read only) into the
    new test directory before anything opens it (phase 7 B.6.2)."""
    import os
    environ = os.environ if environ is None else environ
    workdir = prepare_environment(args.workdir, args.config, environ=environ)
    seed = None
    if seed_from is not None:
        try:
            seed = seed_workdir(seed_from, workdir)
        except IsolationError:
            cleanup(workdir) if created_this_run(workdir) else None
            raise
    budget = budget_from(args)
    models = {role: environ.get(key, "?") for role, key in MODEL_KEYS.items()}
    gate = RealApiGate(environ, input_fn=input_fn, key_prompt=getpass.getpass)
    key = gate.open(args.real, args.api_url, budget, models, assume_yes=args.yes, out=out)
    recorder = CallRecorder(budget, secrets=[key] if key else [])
    image_mock = bool(getattr(args, "image_mock", False))
    if image_mock:
        # phase 7 B.8.1: after the gate (which sets the real API url), before anything uses images
        try:
            path = write_mock_image_config(workdir)
            environ.update({"VIOLET_IMAGE_ENABLED": "1", "VIOLET_IMAGE_PROVIDERS_FILE": str(path),
                            "VIOLET_IMAGE_LEGACY_VISION": "0"})
            validate_image_config(path)
        except IsolationError:
            cleanup(workdir) if created_this_run(workdir) else None
            raise
    from tools.harness import VioletTestSession
    owns_db = None
    if db_factory is not None:
        db, owns_db = db_factory(workdir), True
    session = VioletTestSession(workdir, recorder, real=key is not None, db=db, quiet_hours=args.quiet_hours,
                             no_memory=args.no_memory, verbose=getattr(args, "verbose", False), owns_db=owns_db,
                             image_mock=image_mock)
    session.seed = seed
    out(f"[{'真实模型' if key else '模拟模型'}] 测试目录：{workdir}"
        + ("（图片：离线 Mock，只允许连接文字模型接口）" if image_mock else ""))
    out("只使用虚构的人物和信息。/quit 退出。")
    return session


def usage_line(recorder: CallRecorder) -> str:
    calls = recorder.calls
    errors = len([c for c in calls if not c.ok])
    missing = len([c for c in calls if c.ok and not c.usage_present])
    avg = int(sum(c.latency_ms for c in calls) / len(calls)) if calls else 0
    cost = f"，估算费用 {recorder.cost}" if recorder.cost is not None else ""
    return f"调用 {len(calls)} 次（错误 {errors}），token {recorder.tokens}，平均延迟 {avg} ms{cost}" + \
        (f"；{missing} 次调用没有返回 usage（token 上限对它们无效）" if missing else "") + \
        (f"；已停止：{recorder.stopped}" if recorder.stopped else "")


def run_interactive(session, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> dict:
    transcript: list[dict] = []
    round_no = 0
    while True:
        try:
            line = input_fn("你> ").lstrip("\ufeff").strip()
        except (EOFError, KeyboardInterrupt):
            out("\n已停止。")
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line.startswith("/advance"):
            try:
                hours = float(line.split()[1])
            except (IndexError, ValueError):
                out("用法：/advance 3")
                continue
            session.advance(hours)
            transcript.append({"advance_hours": hours})
            out(f"（时间前进了 {hours} 小时）")
            continue
        if line == "/proactive":
            result = session.proactive_tick()
            transcript.append({"proactive": result})
            out(f"（主动消息：{result['due_reason']}；{result['event'] or '没有触发'}）")
            for text in result["sent"]:
                out(f"Violet> {text}")
            continue
        if line == "/state":
            import json
            out(json.dumps(session.state(), ensure_ascii=False, indent=1, default=str))
            continue
        if line == "/usage":
            out(usage_line(session.recorder))
            continue
        if line.startswith("/score"):
            parts = line.split(maxsplit=2)
            rounds = [t for t in transcript if "user" in t]
            if len(parts) < 2 or parts[1] not in ("0", "1", "2") or not rounds:
                out("用法：/score 0|1|2 备注（给上一轮打分）")
                continue
            rounds[-1]["score"] = int(parts[1])
            rounds[-1]["note"] = parts[2] if len(parts) > 2 else ""
            out("已记录。")
            continue
        round_no += 1
        result = session.say(line, round_no)
        transcript.append({"round": round_no, **result})
        for text in result["violet"] or ["（Violet 这一轮没有发消息）"]:
            out(f"Violet> {text}")
        out(f"   [{len(result['calls'])} 次调用，{result['tokens']} token，{result['reply_latency_ms']} ms"
            + (f"，错误：{result['errors']}" if result["errors"] else "") + "]")
        if session.recorder.stopped:
            out(f"预算已用完，停止：{session.recorder.stopped}")
            break
    return {"transcript": transcript, "usage": usage_line(session.recorder), "state": session.state(),
            "real": session.real, "call_summary": summarize_calls(session.recorder.calls, session.recorder.runs)}


def print_leftovers(out: Callable[[str], None] = print, exclude: Path | None = None) -> list[Path]:
    found = list_leftovers(exclude)
    if found:
        out(f"发现 {len(found)} 个以前遗留的测试目录（没有自动删除，确认后请手动删除）：")
        for path in found:
            out(f"  {path}")
    return found


def main(argv: list[str] | None = None, input_fn: Callable[[str], str] = input,
         db_factory: Callable[[Path], object] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_leftovers:
        if not print_leftovers():
            print("没有遗留的测试目录。")
        return 0
    try:
        session = open_session(args, input_fn=input_fn, db_factory=db_factory)
    except (PermissionError, IsolationError) as exc:
        print(f"未启动：{exc}")
        return 2
    try:
        result = run_interactive(session, input_fn=input_fn)
    except BaseException:
        finish_session(session, args.keep)          # never raises: the original error stays the one reported
        raise
    cleaned = finish_session(session, args.keep)
    path = write_report("chat_transcript.json", result, session.recorder, getattr(args, "output_dir", None))
    print(f"记录已保存：{path}（不含 Key）")
    print_leftovers(exclude=session.workdir)
    return 0 if cleaned else 3


if __name__ == "__main__":
    sys.exit(main())
