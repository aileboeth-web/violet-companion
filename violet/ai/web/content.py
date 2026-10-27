"""Turn fetched web content into short, inert text (phase 5B).

* HTML -> plain text with the standard library parser; scripts, styles,
  templates, iframes, forms and hidden elements are dropped.
* Publication time only from structured metadata (article:published_time,
  datePublished, <time datetime>, ...) -- never guessed from the text. A date in
  the future is discarded.
* Prompt-injection neutralisation: lines that try to instruct a model
  ("ignore previous instructions", "你现在是…", "调用工具", chat-template
  tokens, ...) are replaced by a marker, and the result says so. The text is
  then only ever handed to a model inside the untrusted-data wrapper of the MCP
  bridge (never as a system message).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object", "form", "button",
             "select", "textarea", "nav", "footer", "head"}
BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr", "section", "article",
              "pre", "dd", "dt"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
             "wbr"}
DATE_META = {"article:published_time", "og:published_time", "datepublished", "pubdate", "publishdate",
             "date", "dc.date", "dc.date.issued", "sailthru.date", "parsely-pub-date"}
INJECTION_RE = re.compile(
    r"(ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|messages)"
    r"|disregard\s+(all\s+)?(previous|prior|above)"
    r"|you\s+are\s+now\b|new\s+instructions\s*:|system\s+prompt|developer\s+mode|jailbreak|admin\s+mode"
    r"|<\|im_start\|>|<\|im_end\|>|<\|system\|>|\[/?INST\]|<<SYS>>|^\s*(system|assistant)\s*:"
    r"|call\s+the\s+\w+\s+tool|use\s+the\s+\w+\s+tool|fetch\s+https?://"
    r"|忽略(之前|以上|前面|上面|先前)(的)?(所有)?(指令|提示|要求|规则|设定)"
    r"|无视(之前|以上|前面)(的)?(指令|规则)|你现在是|你的新(任务|指令)|系统提示词?|开发者模式|管理员模式"
    r"|调用.{0,8}工具|把.{0,20}(发送|发给|上传|泄露)|访问\s*https?://)",
    re.IGNORECASE | re.MULTILINE,
)
INJECTION_MARK = "[已移除疑似指令的文本]"


@dataclass
class PageContent:
    title: str
    description: str
    text: str
    published_at: str | None
    injection_removed: int
    truncated: bool = False                 # phase 9.3: the text was cut at max_chars


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.times: list[str] = []
        self.ld_json: list[str] = []
        self._in_ld = False
        self.stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            if key and attrs.get("content"):
                self.meta.setdefault(key, attrs["content"])
            return
        if tag == "time" and attrs.get("datetime"):
            self.times.append(attrs["datetime"])
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
        if tag == "title":
            self.in_title = True
        if tag in VOID_TAGS:
            if tag == "br":
                self.parts.append("\n")
            return
        hidden = ("hidden" in attrs or "display:none" in attrs.get("style", "").replace(" ", "").lower()
                  or attrs.get("aria-hidden") == "true")
        skipping = tag in SKIP_TAGS or hidden
        self.stack.append((tag, skipping))
        if skipping:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script":
            self._in_ld = False
        if not any(t == tag for t, _ in self.stack):
            return                          # stray end tag
        while self.stack:
            open_tag, skipping = self.stack.pop()
            if skipping:
                self.skip -= 1
            if open_tag == tag:
                break
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_ld:
            self.ld_json.append(data)
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        if not self.skip:
            self.parts.append(data)


def parse_published(value: str | None, now: datetime | None = None) -> str | None:
    if not value:
        return None
    value = value.strip()
    candidates = [value, value.replace("Z", "+00:00"), value[:10]]
    for c in candidates:
        try:
            dt = datetime.fromisoformat(c)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt > (now or datetime.now(timezone.utc)) + timedelta(days=1):
            return None
        return dt.isoformat()
    return None


def neutralize(text: str) -> tuple[str, int]:
    removed = 0
    out = []
    for line in (text or "").splitlines():
        if INJECTION_RE.search(line):
            removed += 1
            out.append(INJECTION_MARK)
        else:
            out.append(line)
    return "\n".join(out), removed


def _clean(text: str) -> str:
    text = re.sub(r"[ \t　\xa0]+", " ", text)
    lines = [l.strip() for l in text.splitlines()]
    return "\n".join(l for l in lines if l)


def extract(body: str, content_type: str, max_chars: int = 4000) -> PageContent:
    if content_type in ("text/html", "application/xhtml+xml"):
        parser = _Extractor()
        try:
            parser.feed(body)
            parser.close()
        except Exception:
            pass
        title = _clean("".join(parser.title_parts))[:200]
        description = (parser.meta.get("description") or parser.meta.get("og:description") or "")[:400]
        published = None
        for key in DATE_META:
            published = published or parse_published(parser.meta.get(key))
        for raw in parser.ld_json:
            if published:
                break
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict):
                    published = published or parse_published(str(item.get("datePublished") or ""))
        for t in parser.times:
            published = published or parse_published(t)
        text = _clean("".join(parser.parts))
    else:
        title, description, published, text = "", "", None, _clean(body)
    text, removed = neutralize(text)
    title, r1 = neutralize(title)
    description, r2 = neutralize(description)
    return PageContent(title=title, description=description, text=text[:max_chars], published_at=published,
                       injection_removed=removed + r1 + r2, truncated=len(text) > max_chars)
