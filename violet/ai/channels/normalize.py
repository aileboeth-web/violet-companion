"""What the model reads for each kind of message part (phase 10).

Rules:
  * known meaning is kept in plain words: "[表情：微笑]", "[对方发来一个文件：报告.pdf（Violet 现在打不开文件）]";
  * what cannot be understood is said to be so, never guessed: an unknown QQ face id is "[QQ表情]", an unknown
    message type is "[对方发来一条 Violet 暂时不支持的消息]";
  * raw protocol never reaches the model: CQ codes, platform JSON, XML cards, media ids, URLs of media files;
  * control characters are removed and every text is length-limited.

Images are not rendered here: the runtime downloads them and runs the vision step (the same path WeChat images have
always taken), and only the description enters the turn.
"""

from __future__ import annotations

import re
import unicodedata

from ai.channels.base import Part

MAX_TEXT = 4000
MAX_NAME = 80

# QQ "classic" face ids (OneBot face segment / Telegram has none). Only ids whose meaning is certain are listed;
# anything else is rendered as "[QQ表情]" -- a wrong name would make Violet answer an emotion that was never sent.
QQ_FACES = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞", 7: "闭嘴", 8: "睡", 9: "大哭",
    10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙", 14: "微笑", 15: "难过", 16: "酷", 18: "抓狂", 19: "吐",
    20: "偷笑", 21: "可爱", 22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑",
    29: "悠闲", 30: "奋斗", 31: "咒骂", 32: "疑问", 33: "嘘", 34: "晕", 36: "衰", 37: "骷髅", 38: "敲打",
    39: "再见", 41: "发抖", 42: "爱情", 43: "跳跳", 46: "猪头", 49: "拥抱", 53: "蛋糕", 63: "玫瑰", 64: "凋谢",
    66: "爱心", 67: "心碎", 74: "太阳", 75: "月亮", 76: "赞", 77: "踩", 78: "握手", 79: "胜利", 96: "冷汗",
    97: "擦汗", 98: "抠鼻", 99: "鼓掌", 101: "坏笑", 104: "哈欠", 105: "鄙视", 106: "委屈", 107: "快哭了",
    108: "阴险", 111: "可怜", 124: "OK",
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")
# Telegram puts the bot token in the URL path; OneBot / iLink tokens travel as query parameters or headers
_BOT_TOKEN = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def clean_text(text: str | None, limit: int = MAX_TEXT) -> str:
    """Printable text: control / bidi / zero-width characters removed (emoji and CJK kept), NFC, length-limited."""
    text = unicodedata.normalize("NFC", str(text or ""))
    text = _CONTROL.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > limit:
        text = text[:limit] + "…（后面太长，没有显示）"
    return text


def _name(text: str | None) -> str:
    return clean_text(text, MAX_NAME).replace("\n", " ").strip()


def qq_face_name(face_id) -> str | None:
    try:
        return QQ_FACES.get(int(face_id))
    except (TypeError, ValueError):
        return None


def render_part(part: Part) -> str:
    """One part as the model reads it. Images return "" (the runtime describes them separately)."""
    kind, text = part.kind, part.text
    if kind == "text":
        quote = clean_text(part.data.get("quote"), 200).replace("\n", " ").strip() if part.data.get("quote") else ""
        return (f"[引用：{quote}]\n" if quote else "") + clean_text(text)
    if kind == "emoji":
        name = _name(text)
        return f"[表情：{name}]" if name else f"[{part.data.get('platform_label') or ''}表情]"
    if kind == "sticker":
        name = _name(text)
        return f"[表情包：{name}]" if name else "[表情包]"
    if kind == "voice":
        transcript = clean_text(text, 1000).strip()
        if transcript:
            return f"[语音，转成文字是：{transcript}]"
        return "[对方发来一段语音（Violet 现在听不了语音，也没有转成文字）]"
    if kind == "file":
        name = _name(text)
        return f"[对方发来一个文件{('：' + name) if name else ''}（Violet 现在打不开文件）]"
    if kind == "video":
        return "[对方发来一段视频（Violet 现在看不了视频）]"
    if kind == "location":
        name = _name(text)
        return f"[对方分享了一个位置{('：' + name) if name else ''}]"
    if kind == "link":
        title = _name(text)
        url = str(part.data.get("url") or "")
        url = url if re.match(r"^https?://[^\s]{1,300}$", url) else ""
        return f"[对方分享了一个链接{('：' + title) if title else ''}{(' ' + url) if url else ''}]"
    if kind == "quote":
        quoted = clean_text(text, 200).replace("\n", " ").strip()
        return f"[引用：{quoted}]" if quoted else "[引用了一条消息]"
    if kind == "mention":
        name = _name(text)
        return f"@{name}" if name else ""
    if kind == "image":
        return ""
    return "[对方发来一条 Violet 暂时不支持的消息]"


def render_parts(parts: list[Part]) -> str:
    """The parts that are not images, joined the way a person would read them (inline pieces stay on one line)."""
    inline = ("text", "emoji", "mention")
    out: list[str] = []
    previous = None
    for part in parts:
        piece = render_part(part)
        if not piece:
            continue
        if out and part.kind in inline and previous in inline:
            out[-1] = out[-1] + piece
        else:
            out.append(piece)
        previous = part.kind
    return "\n".join(p.strip("\n") for p in out if p.strip())


def coalesce(parts: list[Part]) -> list[Part]:
    """One text part per run of non-image parts (a QQ message "哈哈[微笑]好的" is one line, not three inbox rows);
    images stay separate parts in their place, so the turn keeps the order the person sent things in."""
    out: list[Part] = []
    run: list[Part] = []

    def flush() -> None:
        if run:
            text = render_parts(run)
            if text:
                out.append(Part("text", text, {"rendered": True}))
            run.clear()

    for part in parts:
        if part.kind == "image":
            flush()
            out.append(part)
        else:
            run.append(part)
    flush()
    return out


def safe_error(exc: BaseException | str) -> str:
    """An error for status pages and logs: class name + message with URLs, IPs, paths and tokens removed."""
    from ai.mcp import safe_error_text
    if isinstance(exc, BaseException):
        text = f"{exc.__class__.__name__}: {exc}"
    else:
        text = str(exc)
    text = _BOT_TOKEN.sub("bot<redacted>", text)
    text = safe_error_text(text)
    return _LONG_TOKEN.sub("<redacted>", text)[:300]
