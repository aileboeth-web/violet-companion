"""Links in Violet's reply must come from somewhere real (phase 9.3).

A model can write a plausible link it never saw ("来源：https://news.example.org/2026/..."). Before a reply is sent,
every http(s) link in it is checked against what this turn actually had: the user's message, the tool results of this
run (search results, read pages, the Performer's tool data) and the recent conversation. A link found in none of them
is removed (the sentence stays) and recorded -- a source is never invented, and nothing is added in its place.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from ai.web.findings import domain_of

logger = logging.getLogger("violet.web")

URL_RE = re.compile(r"https?://[^\s\"'<>，。、；！？）)\]】]+", re.I)


def _norm(url: str) -> str:
    return url.strip().rstrip("/.,;:!?").lower()


def known_urls(texts: Iterable[str | None]) -> set[str]:
    found = set()
    for text in texts:
        for url in URL_RE.findall(text or ""):
            found.add(_norm(url))
    return found


def drop_unverified_links(reply: str, known: set[str]) -> tuple[str, list[str]]:
    removed: list[str] = []

    def check(match: re.Match) -> str:
        url = match.group(0)
        if _norm(url) in known:
            return url
        removed.append(url)
        return ""
    text = URL_RE.sub(check, reply or "")
    if not removed:
        return reply, []
    text = re.sub(r"[（(]\s*[)）]", "", text)                      # "（）" left by a removed link
    lines = [line.rstrip() for line in text.splitlines()]
    # a short label left alone on its line ("来源：", "官方公告：") pointed at the removed link
    text = "\n".join(line for line in lines if line.strip() and not re.fullmatch(r"[^：:\n]{0,12}[：:]", line.strip()))
    logger.warning("[web] removed %d link(s) not seen this turn: %s", len(removed),
                   ", ".join(sorted({domain_of(u) or "?" for u in removed})))
    return text, removed


def checked_reply(chat_result: dict[str, Any], user_message: str, extra_texts: Iterable[str | None] = ()) -> dict[str, Any]:
    """The chat result with links it could not have seen removed (unchanged when there are none)."""
    content = chat_result.get("content") or ""
    if not URL_RE.search(content):
        return chat_result
    tool_texts = [m.get("content") for m in chat_result.get("messages") or []
                  if isinstance(m, dict) and m.get("role") in ("tool", "user") and isinstance(m.get("content"), str)]
    known = known_urls([user_message, *tool_texts, *extra_texts])
    text, removed = drop_unverified_links(content, known)
    if not removed:
        return chat_result
    return {**chat_result, "content": text, "unverified_links_removed": removed}
