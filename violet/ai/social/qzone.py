"""QQ 空间 posts ("说说") of the channel "QQ" (0.7.9, user: "也不可以发动态").

  publish()   the logged-in account's web cookies from NapCat (get_cookies, never logged or shown) -> QQ 空间's own
              publish address (emotion_cgi_publish_v6, the request the web page sends) -> the post's id
  compose()   one model call: her persona + what happened in her (virtual) life lately + her last posts -> a JSON
              {"post": true/false, "text": "..."}; she may decide there is nothing worth posting
  tick()      the social engine calls it every ~25 minutes: only when VIOLET_QZONE_ENABLED is on, between 09:00 and
              22:59, at most VIOLET_QZONE_DAILY posts a day (0 = no daily limit) and never twice within 4 hours,
              and then only now and then (a chance, so the time of day varies)

The owner's private things never go into a post: the prompt does not contain them (only her persona and her own
virtual life), and the rule is repeated in it. Every post (sent or failed) is in social_posts and on the channels
page; the owner's chat learns about her posts through the owner digest.
"""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any

import requests

from ai import trace
from ai.settings import env_bool, env_int

logger = logging.getLogger("violet.social.qzone")
PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MIN_GAP_HOURS = 4
CHANCE = 0.3

COMPOSE_RULES = """【QQ 空间动态】
你在想要不要在自己的 QQ 空间发一条动态（说说）。像真人一样：只有真有想分享的小事、心情或想法时才发，没有就不发。
- 素材只能来自你自己：你的性格、兴趣，和下面列出的你最近的生活。不要编造列表里没有的经历。
- 绝对不写你主人（和你私聊的那个人）的任何事，也不提和谁私聊过；不 @ 任何人，不发链接、联系方式。
- 一到三句，口语，可以带一个 emoji；不要像广告、不要像日记模板，不要和你最近发过的动态重复。
【你的回答】只输出一个 JSON 对象：{"post": true 或 false, "text": "要发的内容（post 为 false 时留空）", "reason": "一句话，只给管理员看"}"""


class QzoneError(RuntimeError):
    pass


def enabled() -> bool:
    return env_bool("VIOLET_QZONE_ENABLED", False)


def gtk(key: str) -> int:
    """QQ 空间's request token, computed from the p_skey cookie (the same function its web page runs)."""
    value = 5381
    for ch in key:
        value += (value << 5) + ord(ch)
    return value & 0x7fffffff


def publish(adapter, text: str, session: requests.Session | None = None) -> str:
    """Post `text` as the logged-in account. Returns the post id; raises QzoneError (without cookie values)."""
    text = (text or "").strip()
    if not text:
        raise QzoneError("动态内容是空的")
    cookies = adapter.cookies("qzone.qq.com")
    uin = re.sub(r"\D", "", cookies.get("uin") or cookies.get("p_uin") or str(getattr(adapter, "self_id", "") or ""))
    uin = str(int(uin)) if uin else ""                     # the cookie writes "o0012345"
    key = cookies.get("p_skey") or cookies.get("skey")
    if not uin or not key:
        raise QzoneError("没拿到 QQ 空间的登录信息（QQ 可能需要重新扫码登录）")
    jar = {k: v for k, v in cookies.items() if not k.startswith("_")}
    form = {"syn_tweet_verson": "1", "paramstr": "1", "who": "1", "con": text[:1000], "feedversion": "1", "ver": "1",
            "ugc_right": "1", "to_sign": "0", "hostuin": uin, "code_version": "1", "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{uin}"}
    headers = {"User-Agent": UA, "Referer": f"https://user.qzone.qq.com/{uin}", "Origin": "https://user.qzone.qq.com"}
    http = session or requests.Session()
    try:
        response = http.post(PUBLISH_URL, params={"g_tk": gtk(key), "uin": uin}, data=form, cookies=jar,
                             headers=headers, timeout=(10, 30))
    except requests.RequestException as exc:
        raise QzoneError(f"连不上 QQ 空间：{exc.__class__.__name__}") from None
    body = response.text or ""
    match = re.search(r"\{.*\}", body, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if response.status_code != 200 or data.get("code") not in (0, "0"):
        message = str(data.get("message") or data.get("msg") or f"HTTP {response.status_code}")[:120]
        raise QzoneError(f"QQ 空间没有接受：{message}")
    return str(data.get("tid") or data.get("t1_tid") or "")


def _napcat(runtime):
    manager = getattr(runtime, "channels", None)
    if manager is None:
        return None
    try:
        entries = manager.entries()
    except Exception:                                      # noqa: BLE001
        return None
    for channel_id, entry in entries.items():
        if entry.get("type") == "napcat" and entry.get("enabled", True):
            adapter = manager.get(channel_id)
            if adapter is not None and hasattr(adapter, "cookies"):
                return adapter
    return None


def _material(db) -> list[str]:
    since = (datetime.fromisoformat(db.now()[:19]) - timedelta(days=3)).isoformat(sep=" ")
    rows = db.fetch_all("SELECT content, occurred_at FROM life_events WHERE status = 'happened' AND occurred_at >= ? "
                        "ORDER BY occurred_at DESC LIMIT 6", (since,))
    return [f"- {str(r['occurred_at'])[5:16]} {r['content']}" for r in rows]


def compose(db) -> dict[str, Any]:
    """{"post": bool, "text": str, "reason": str, "model": str}. Raises QzoneError."""
    from ai.social import llm
    from ai.social.engine import _persona_text
    recent = db.fetch_all("SELECT content, created_at FROM social_posts WHERE status = 'sent' ORDER BY id DESC LIMIT 5")
    now = db.now()
    lines = [f"现在是 {now[:16].replace('T', ' ')}。", "", "【你最近的生活】", *(_material(db) or ["（这几天没有记下特别的事）"]),
             "", "【你最近发过的动态】", *([f"- {str(r['created_at'])[5:16]} {r['content']}" for r in recent] or ["（还没发过）"]),
             "", "决定这次发不发，只输出 JSON。"]
    try:
        decision, model, _ = llm.decide(_persona_text(db) + "\n\n" + COMPOSE_RULES, "\n".join(lines))
    except llm.SocialLLMError as exc:
        raise QzoneError(str(exc)) from None
    text = " ".join(str(decision.get("text") or "").split())[:300]
    return {"post": bool(decision.get("post")) and bool(text), "text": text,
            "reason": str(decision.get("reason") or "")[:200], "model": model}


def record(db, channel: str, content: str, *, status: str, origin: str, tid: str | None = None,
           reason: str | None = None, error: str | None = None) -> int:
    cursor = db.execute("INSERT INTO social_posts (channel, content, status, origin, tid, reason, error, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (channel, content, status, origin, tid, reason, error, db.now()))
    return int(cursor.lastrowid)


def post_now(runtime, text: str, origin: str = "manual", reason: str | None = None) -> dict[str, Any]:
    adapter = _napcat(runtime)
    if adapter is None or not adapter.is_running():
        raise QzoneError("QQ 没有连接")
    try:
        tid = publish(adapter, text)
    except QzoneError as exc:
        record(runtime.db, adapter.channel_id, text, status="failed", origin=origin, reason=reason, error=str(exc))
        trace.step("QQ 动态", 结果="失败", 原因=str(exc))
        raise
    post_id = record(runtime.db, adapter.channel_id, text, status="sent", origin=origin, tid=tid, reason=reason)
    trace.step("QQ 动态", 结果="已发", 内容=trace.text(text))
    return {"id": post_id, "tid": tid, "text": text}


def due(db, now: datetime | None = None) -> str | None:
    """None when she may post now, else why not (for the tick and the page)."""
    if not enabled():
        return "发动态没有打开"
    now = now or datetime.fromisoformat(db.now()[:19])
    if not 9 <= now.hour <= 22:
        return "只在 9:00 到 23:00 之间发"
    day = now.date().isoformat()
    limit = env_int("VIOLET_QZONE_DAILY", 1)
    sent = db.fetch_one("SELECT COUNT(*) AS n FROM social_posts WHERE status = 'sent' AND origin = 'auto' "
                        "AND created_at >= ?", (day,))["n"]
    if limit and sent >= limit:
        return "今天已经发够了"
    last = db.fetch_one("SELECT MAX(created_at) AS at FROM social_posts WHERE status = 'sent'")
    if last and last["at"]:
        try:
            if now - datetime.fromisoformat(str(last["at"])[:19]) < timedelta(hours=MIN_GAP_HOURS):
                return f"离上一条不到 {MIN_GAP_HOURS} 小时"
        except ValueError:
            pass
    return None


def tick(runtime, rng: random.Random | None = None) -> dict[str, Any] | None:
    """One look: maybe she posts. Never raises."""
    try:
        if due(runtime.db) or (rng or random).random() >= CHANCE:
            return None
        adapter = _napcat(runtime)
        if adapter is None or not adapter.is_running():
            return None
        idea = compose(runtime.db)
        if not idea["post"]:
            trace.step("QQ 动态", 结果="这次不发", 理由=idea["reason"])
            return None
        return post_now(runtime, idea["text"], origin="auto", reason=idea["reason"])
    except QzoneError as exc:
        logger.info("qzone post skipped: %s", exc)
    except Exception:
        logger.exception("qzone tick failed")
    return None
