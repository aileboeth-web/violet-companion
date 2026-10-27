"""Information kinds and interfaces for later phases (phase 5A: interfaces only).

Nothing here touches the internet. Phase 5B (Web MCP) will implement
ExternalFactProvider; this module fixes the contract it must follow.

Four kinds of information are kept apart everywhere:

  PUBLIC_FACT        what a public web page / news item says (the world's business).
                     Stored in its own table (5B), labelled untrusted. NEVER a life
                     event, never "I did ...". Violet may say "我看到一篇文章说…".
  VIOLET_VIRTUAL_EVENT  Violet's virtual life (life_events, is_virtual=1). Citable as her
                     past only when status == 'happened'.
  USER_REAL          what the user lived (event_memories / user_profile_facts).
                     Never Violet's experience, never a life event.
  EXTERNAL_READING   the bridge between them: "Violet found and read this article" is a
                     life event (event_type 'reading_record', origin 'reading',
                     external_ref = url). Only the act of reading is Violet's; what the
                     article describes stays a PUBLIC_FACT.
  AI_GENERATED       generated images and their captions (later phase); always
                     flagged, never presented as real photos.

ConsistencyGuard.check_event enforces this: external origins / public facts /
user experiences are rejected as life events; a reading_record must describe
only the act of reading ("读了一篇讲…的文章") and carry its source url.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlparse


class InfoKind(str, Enum):
    PUBLIC_FACT = "public_fact"
    VIOLET_VIRTUAL_EVENT = "violet_virtual_event"
    USER_REAL = "user_real"
    EXTERNAL_READING = "external_reading"
    AI_GENERATED = "ai_generated"


READING_EVENT_TYPE = "reading_record"
READING_ORIGIN = "reading"
# The whole event text must be the act of reading, nothing the article describes.
READING_ACT_RE = re.compile(
    r"(?:读了|看了|翻了|读到|看到|发现)(?:一篇|一则|一段|一个|篇|则)?[^，,。；;！!？?]{0,24}"
    r"(?:文章|报道|帖子|博客|介绍|评论|专栏|访谈|短文|书评|乐评)")
MAX_READING_CONTENT_CHARS = 40


@dataclass(frozen=True)
class PublicFact:
    """A statement found on the public internet. Untrusted, third-person, never Violet's life."""
    url: str
    title: str
    summary: str
    fetched_at: str
    domain: str = ""
    published_at: str | None = None
    retrieved_via: str = "web_mcp"          # provider / tool name
    trust: str = "untrusted"
    kind: InfoKind = InfoKind.PUBLIC_FACT

    @property
    def fact_id(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]

    def __post_init__(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("a public fact needs an http(s) url")


@dataclass(frozen=True)
class ReadingRecord:
    """Violet found and read a public article: the reading is hers, the content is not."""
    fact: PublicFact
    topic: str                              # short noun phrase chosen by code, e.g. "爵士乐历史"
    read_at: str
    thread_id: int | None = None
    kind: InfoKind = InfoKind.EXTERNAL_READING

    def content(self) -> str:
        return f"读了一篇讲{self.topic}的文章"[:MAX_READING_CONTENT_CHARS]

    def to_event_proposal(self) -> dict[str, Any]:
        """A life-event proposal; it still has to pass ConsistencyGuard.check_event."""
        return {
            "thread_id": self.thread_id, "plan_id": None, "event_type": READING_EVENT_TYPE,
            "content": self.content(), "occurred_at": self.read_at, "duration_minutes": 30,
            "source": "life_engine:reading",
            "source_detail": {"origin": READING_ORIGIN, "info_kind": self.kind.value, "external_ref": self.fact.url,
                              "fact_id": self.fact.fact_id, "title": self.fact.title[:120]},
        }


def check_reading_record(proposal: dict[str, Any]) -> list[str]:
    """Names of failed checks for a reading_record proposal (empty = ok)."""
    problems = []
    detail = proposal.get("source_detail") or {}
    content = (proposal.get("content") or "").strip()
    if proposal.get("event_type") != READING_EVENT_TYPE or detail.get("origin") != READING_ORIGIN:
        problems.append("reading_record_invalid")
    url = str(detail.get("external_ref") or "")
    if urlparse(url).scheme not in ("http", "https"):
        problems.append("reading_record_without_source")
    if len(content) > MAX_READING_CONTENT_CHARS or not READING_ACT_RE.fullmatch(content):
        problems.append("reading_record_not_a_reading_act")
    return problems


class ExternalFactProvider(Protocol):
    """Web MCP adapter (phase 5B). Low frequency, budget limited, results untrusted."""

    def search(self, query: str, *, recency_days: int | None = None, limit: int = 5) -> list[PublicFact]: ...

    def fetch(self, url: str) -> PublicFact | None: ...


class NullFactProvider:
    """The only provider in phase 5A: never touches the network."""

    def search(self, query: str, *, recency_days: int | None = None, limit: int = 5) -> list[PublicFact]:
        return []

    def fetch(self, url: str) -> PublicFact | None:
        return None


@dataclass(frozen=True)
class GeneratedImage:
    path: str
    caption: str
    prompt: str
    model: str
    life_event_id: int | None
    ai_generated: bool = True
    kind: InfoKind = InfoKind.AI_GENERATED
    extra: dict[str, Any] = field(default_factory=dict)


class VisualEngine(Protocol):
    """Image generation adapter (later phase): only happened life events or scenes marked hypothetical."""

    def render(self, life_event: dict[str, Any], visual_profile: dict[str, Any]) -> GeneratedImage: ...
