"""The channel of the turn being processed (phase 10).

The runtime sets it around every pipeline run (and the proactive scheduler around every proactive run, with the
channel the message will go to). Chat / Proactive read it so Violet knows where she is talking: the prompt and the
persona were written for WeChat ("正在和对方用微信聊天", "每条微信消息单独占一行"); on another platform those words
name the wrong app. ``adapt_for_channel`` replaces the platform's name in the system text -- the same text, the same
rules -- and the context gains one line saying which platform this is. On WeChat nothing changes.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelContext:
    channel: str = "wechat"
    channel_type: str = "wechat_ilink"
    label: str = "微信"          # what the platform is called in Chinese


LABELS = {"wechat_ilink": "微信", "telegram": "Telegram", "onebot11": "QQ", "qqbot": "QQ", "webhook": "聊天应用",
          "local": "本地测试"}
DEFAULT = ChannelContext()
_CURRENT: contextvars.ContextVar[ChannelContext] = contextvars.ContextVar("violet_channel", default=DEFAULT)


def label_for(channel_type: str, configured: str | None = None) -> str:
    return (configured or "").strip()[:20] or LABELS.get(channel_type, "聊天应用")


def current() -> ChannelContext:
    return _CURRENT.get()


@contextlib.contextmanager
def channel_scope(context: ChannelContext | None):
    token = _CURRENT.set(context or DEFAULT)
    try:
        yield context or DEFAULT
    finally:
        _CURRENT.reset(token)


def adapt_for_channel(text: str, context: ChannelContext | None = None) -> str:
    """The system text with "微信" read as the platform of this conversation (unchanged on WeChat)."""
    context = context or current()
    if not text or context.channel_type == "wechat_ilink":
        return text
    label = context.label
    return (text.replace("每条微信消息", "每条消息").replace("一条微信", "一条消息")
            .replace("微信聊天", f"{label}聊天").replace("用微信", f"用{label}").replace("微信", label))


def channel_note(context: ChannelContext | None = None) -> str | None:
    """One context line about the platform (None on WeChat: the prompt already says it)."""
    context = context or current()
    if context.channel_type == "wechat_ilink":
        return None
    return (f"【这次聊天的地方】你们现在是在{context.label}上聊天（不是微信）。照常聊天，"
            f"不要提只有微信才有的东西（拍一拍、朋友圈、微信表情名之类）。")
