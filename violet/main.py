import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai.settings import ENV_PATH, env_float, env_int, env_str, setup_logging

setup_logging()

from wx_ilink_py import ILinkClient, create_app
from ai.chat import violetdb, run_pipeline
from ai.agents import IntentAgent, PerformerAgent, ChatAgent, MemoryAgent, ProactiveAgent
from ai.debug_visualizer import reload_debug_mode
from ai.life_scheduler import LifeScheduler
from ai.proactive_scheduler import ProactiveScheduler
from ai.vision import describe_image
from violet_runtime import (
    VioletRuntime,
    IMAGE_VISION_FAILED_TEXT,
    is_incomplete as _is_incomplete,  # noqa: F401  (kept for backward compatibility)
)
from threading import Event, Thread
from dotenv import load_dotenv
import atexit
import ipaddress
import logging
import time

logger = logging.getLogger("violet.main")

_ENV_PATH = str(ENV_PATH)

atexit.register(violetdb.close)

# Phase 9.0: settings saved in the Violet Control Center (data/control/) go on top of .env before anything reads them
from ai.control_config import apply_overrides  # noqa: E402
apply_overrides()

client = ILinkClient(data_dir=env_str("VIOLET_DATA_DIR", "./data"), auto_login=False)

# --- Agent 实例（持久化，MCP 不重复启动）---
intent_agent = IntentAgent()
performer_agent = PerformerAgent()
chat_agent = ChatAgent()
memory_agent = MemoryAgent()
proactive_agent = ProactiveAgent()

_ALL_AGENTS = [intent_agent, performer_agent, chat_agent, memory_agent, proactive_agent]

runtime: VioletRuntime | None = None


def _send_pipeline_messages(messages: list[dict], proactive_event_id: int | None = None) -> dict:
    """Send messages through the outbox. Returns a report with sent/failed counts.

    The proactive scheduler records the final event status from this report.
    """
    if proactive_event_id is not None:
        return runtime.send_proactive(messages, proactive_event_id)
    return runtime.sender.send_messages(messages, origin="reply").to_dict()


proactive_scheduler = ProactiveScheduler(
    proactive_agent=proactive_agent,
    chat_agent=chat_agent,
    performer_agent=performer_agent,
    send_messages=_send_pipeline_messages,
)

# Violet's virtual life has its own clock: independent of WeChat login and of proactive chat.
life_scheduler = LifeScheduler(violetdb)

runtime = VioletRuntime(
    client=client,
    db=violetdb,
    agents={
        "intent": intent_agent,
        "chat": chat_agent,
        "performer": performer_agent,
        "memory": memory_agent,
    },
    proactive_scheduler=proactive_scheduler,
    pipeline_runner=run_pipeline,
)

# Phase 10: every chat platform (WeChat iLink, Telegram, QQ via OneBot ...) is a channel of this one runtime
from ai.channels.manager import ChannelManager  # noqa: E402
channel_manager = ChannelManager(runtime, data_dir=env_str("VIOLET_DATA_DIR", "./data"))


def _load_debounce_config() -> tuple[float, float]:
    """从 .env 读取 DEBOUNCE_NORMAL / DEBOUNCE_EXTRA，返回 (normal, extra)。"""
    return env_float("DEBOUNCE_NORMAL", 5.0), env_float("DEBOUNCE_EXTRA", 10.0)


def reload_debounce_config() -> None:
    """重新从 .env 加载防抖参数，在 /reload-env 时调用。"""
    from ai.control_config import reload_env
    reload_env()                                  # phase 9.0: .env, then the Control Center's saved settings
    normal, extra = _load_debounce_config()
    runtime.buffer.set_delays(normal, extra)
    logger.info("[debounce] reloaded: normal=%ss extra=%ss", normal, extra)


def reload_runtime_switches() -> None:
    """Phase 9.0.1: switches the Control Center labels "immediate" really are. VIOLET_SIMULATE_TYPING was read only when
    VioletRuntime was built; the life thread only started if VIOLET_LIFE_ENGINE was on at boot (off already stopped it at
    the next tick). start() does nothing while the engine is off or the thread already runs."""
    from ai.settings import env_bool
    runtime.simulate_typing = env_bool("VIOLET_SIMULATE_TYPING", True)
    if _LIFE_STARTED.is_set():
        life_scheduler.start()


_LIFE_STARTED = Event()            # set by __main__ once the life scheduler may run (not when tests import main)

app = create_app(
    client=client,
    violetdb=violetdb,
    proactive_scheduler=proactive_scheduler,
    all_agents=_ALL_AGENTS,
    reload_debug_mode=reload_debug_mode,
    extra_reload_hooks=[reload_debounce_config, reload_runtime_switches],
    runtime=runtime,
)


# ---------------------------------------------------------------------------
# Backward-compatible entry points (the logic now lives in violet_runtime.py)
# ---------------------------------------------------------------------------

def Eve_Message(message: str):
    """Receive an incoming text and add it to the debounce buffer (durably)."""
    runtime.ingest_text(message)


def Eve_File(filetype, filePath):
    """处理微信媒体消息。图片先交给 vision 模型识别，再进入正常聊天流程；其他类型暂不处理。"""
    if filetype != 2:  # 2 = IMAGE
        logger.info("[media] 暂不处理媒体类型: %s", filetype)
        return
    try:
        text = f"[用户发来一张图片]\n图片内容：{describe_image(filePath)}"
    except Exception as exc:
        logger.warning("[vision] 图片识别失败: %s", exc)
        text = IMAGE_VISION_FAILED_TEXT
    runtime.ingest_text(text)


def _start_daily_decay_scheduler():
    """在后台线程中，每天凌晨 03:00（VIOLET_TIMEZONE 的时间，不是服务器系统时间）调用 decay_event_memories。"""
    from ai.settings import seconds_until_daily

    def _loop():
        while True:
            time.sleep(seconds_until_daily(3))
            try:
                result = violetdb.decay_event_memories()
                logger.info("[decay] daily memory decay done: %s", result)
            except Exception:
                logger.exception("[decay] error")
            try:
                from ai.images.media import MediaAssetStore
                store = MediaAssetStore(violetdb)
                logger.info("[media] expired images removed: %d, old image texts cleared: %d",
                            store.purge_expired(), store.purge_old_texts())
            except Exception:
                logger.exception("[media] retention clean-up failed")
    Thread(target=_loop, name="violet-decay", daemon=True).start()


def _send_banner_when_reachable(banner: str, stop: Event | None = None) -> None:
    """The startup notice goes to the owner's channel once it can be delivered (WeChat: after the QR login)."""
    while stop is None or not stop.is_set():
        channel, _peer = runtime.proactive_conversation()
        if channel != "wechat" or runtime.wechat_logged_in():
            try:
                runtime.send_system_text(banner)
            except Exception:
                logger.exception("startup message failed")
            return
        time.sleep(2)


def _start_services() -> None:
    """Recovery, turn worker, channels, startup notice, scheduler and decay. Phase 10: Violet Core no longer waits for
    the WeChat QR login -- other channels and the Control Center work without it; WeChat polling starts receiving as
    soon as it is logged in."""
    runtime.recover_pending()
    runtime.start_worker()
    started = channel_manager.start_enabled()
    logger.info("[channels] started: %s", ", ".join(started) or "none")
    banner = env_str("VIOLET_STARTUP_MESSAGE", "Violet Online.")
    if banner:
        Thread(target=_send_banner_when_reachable, args=(banner,), name="violet-banner", daemon=True).start()
    proactive_scheduler.start()
    _start_daily_decay_scheduler()


def mainLoop():
    """Start services (non-blocking: every channel runs its own supervised thread)."""
    _start_services()


def _dashboard_tls() -> tuple[str, str] | None:
    """Phase 10.4: (cert, key) when Violet serves HTTPS itself (VIOLET_DASHBOARD_TLS_CERT / _KEY, both readable files)."""
    cert, key = env_str("VIOLET_DASHBOARD_TLS_CERT", ""), env_str("VIOLET_DASHBOARD_TLS_KEY", "")
    if not (cert or key):
        return None
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        logger.error("VIOLET_DASHBOARD_TLS_CERT / _KEY do not both point to files: HTTPS is off")
        return None
    return cert, key


def _dashboard_bind() -> tuple[str, int]:
    """A loopback address unless Violet serves HTTPS itself. Phase 9.0.1: plain HTTP is never opened on another
    interface -- no flag can prove that HTTPS is in front. Phase 10.4: with a certificate (VIOLET_DASHBOARD_TLS_CERT /
    _KEY, made by the installer for LAN / public access) the dashboard may listen on any address; the session cookie
    is then HTTPS-only. Otherwise remote access goes through an SSH tunnel or an HTTPS reverse proxy on the same
    machine (VIOLET_DASHBOARD_TRUST_PROXY=1, VIOLET_DASHBOARD_COOKIE_SECURE=1)."""
    host = env_str("VIOLET_DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1"
    port = env_int("VIOLET_DASHBOARD_PORT", 7100)
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and _dashboard_tls() is None:
        logger.error("VIOLET_DASHBOARD_HOST=%s is not a loopback address and HTTPS is not configured: listening on "
                     "127.0.0.1 only. For remote access install with --access public (HTTPS), use an SSH tunnel or an "
                     "HTTPS reverse proxy on this machine (see tools/README §21).", host)
        return "127.0.0.1", port
    return host, port


def _served_app():
    """The WSGI app as served. With VIOLET_DASHBOARD_TRUST_PROXY=1 the client address and scheme come from
    X-Forwarded-For / -Proto, but only for connections from this machine (the reverse proxy)."""
    from ai.settings import env_bool
    if env_bool("VIOLET_DASHBOARD_TRUST_PROXY", False):
        from wx_ilink_py.auth import trusted_proxy_app
        return trusted_proxy_app(app)
    return app


if __name__ == "__main__":
    import signal
    import socket
    from werkzeug.serving import make_server

    # phase 10.7: one Violet per data directory -- a second process would answer every message twice
    from ai.instance_lock import AlreadyRunning, InstanceLock
    try:
        _instance_lock = InstanceLock(env_str("VIOLET_DATA_DIR", "./data")).acquire()
    except AlreadyRunning as exc:
        logger.error("%s", exc)
        print(exc, file=sys.stderr)
        sys.exit(3)

    _LIFE_STARTED.set()
    life_scheduler.start()

    if channel_manager.entry("wechat").get("enabled", True) and client._load_credentials():
        try:
            client.ensure_login()
        except Exception:
            logger.exception("WeChat login with the saved credentials failed; scan the QR code in the Control Center")
    mainLoop()

    host, port = _dashboard_bind()
    # request lines carry search terms and ids of private data: no access log lines (errors still go to the log)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    tls = _dashboard_tls()
    ssl_context = None
    if tls is not None:
        import ssl
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(*tls)
    server = make_server(host, port, _served_app(), threaded=True, ssl_context=ssl_context)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    logger.info("Dashboard listening on %s://%s:%s", "https" if tls else "http", host, port)

    def _handle_sigterm(signum, frame):
        logger.info("[main] SIGTERM received, shutting down server...")
        Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    if hasattr(signal, "SIGBREAK"):              # Windows: tools/violet_service.py stops Violet with CTRL_BREAK
        signal.signal(signal.SIGBREAK, _handle_sigterm)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[main] KeyboardInterrupt, shutting down server...")
        server.shutdown()
    life_scheduler.stop()
    channel_manager.stop_all()
    runtime.stop()
    sys.exit(0)
