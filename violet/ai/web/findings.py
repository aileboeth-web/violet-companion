"""web_findings / web_sources / web_requests (phase 5B).

A finding is "what one public page said", kept per URL with its domain,
query, fetch time, publication time (verified only when read from the page's
own metadata), trust label and expiry. It is never a user memory and never a
life event. Several findings on one topic stay separate rows -- conflicting
sources are not merged.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable
from urllib.parse import urlsplit

from ai.memory_model import parse_time


def domain_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


class FindingsStore:
    def __init__(self, db):
        self.db = db

    # -- sources ---------------------------------------------------------------------
    def source(self, domain: str) -> dict[str, Any] | None:
        domain = domain.lower()
        parts = domain.split(".")
        # a rule for "example.org" also covers "news.example.org"
        for i in range(len(parts) - 1):
            row = self.db.fetch_one("SELECT * FROM web_sources WHERE domain = ?", (".".join(parts[i:]),))
            if row:
                return row
        return None

    def domain_disabled(self, domain: str) -> bool:
        row = self.source(domain)
        return bool(row and row["status"] == "disabled")

    def set_source(self, domain: str, status: str, reason: str | None = None, trust: str | None = None) -> dict[str, Any]:
        if status not in ("enabled", "disabled"):
            raise ValueError("status must be enabled or disabled")
        domain = domain_of("http://" + domain.strip().lower()) or domain.strip().lower()
        self.db.execute(
            "INSERT INTO web_sources (domain, status, trust, reason, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET status = excluded.status, trust = COALESCE(excluded.trust, web_sources.trust), "
            "reason = excluded.reason, updated_at = excluded.updated_at",
            (domain, status, trust, reason, self.db.now()))
        return self.db.fetch_one("SELECT * FROM web_sources WHERE domain = ?", (domain,))

    def sources(self) -> list[dict[str, Any]]:
        return self.db.fetch_all("SELECT * FROM web_sources ORDER BY domain")

    # -- findings --------------------------------------------------------------------
    def get(self, finding_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM web_findings WHERE id = ?", (finding_id,))

    def by_url(self, url: str) -> dict[str, Any] | None:
        return self.db.fetch_one("SELECT * FROM web_findings WHERE url = ?", (url,))

    def list(self, limit: int = 100, status: str | None = None, origin: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if origin:
            clauses.append("origin = ?")
            params.append(origin)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.db.fetch_all(f"SELECT * FROM web_findings {where} ORDER BY id DESC LIMIT ?", (*params, limit))

    def upsert(self, *, url: str, title: str, summary: str, source_kind: str, origin: str, provider: str | None = None,
               query: str | None = None, interest_key: str | None = None, published_at: str | None = None,
               published_verified: bool = False, injection_removed: int = 0, ttl_days: float = 14) -> int:
        now = self.db.now()
        expires = (parse_time(now) + timedelta(days=ttl_days)).isoformat()
        domain = domain_of(url)
        source = self.source(domain)
        trust = "suspicious" if injection_removed else ((source or {}).get("trust") or "untrusted")
        existing = self.by_url(url)
        if existing:
            verified = bool(existing["published_verified"]) and not published_verified
            self.db.execute(
                "UPDATE web_findings SET title = ?, summary = COALESCE(NULLIF(?, ''), summary), "
                "source_kind = CASE WHEN ? = 'fetched_page' THEN 'fetched_page' ELSE source_kind END, "
                "published_at = ?, published_verified = ?, trust = ?, injection_removed = MAX(injection_removed, ?), "
                "fetched_at = ?, expires_at = ?, updated_at = ? WHERE id = ?",
                (title or existing["title"], summary, source_kind,
                 existing["published_at"] if verified else (published_at or existing["published_at"]),
                 1 if (published_verified or verified) else 0, trust, injection_removed, now, expires, now,
                 existing["id"]))
            return int(existing["id"])
        cursor = self.db.execute(
            """INSERT INTO web_findings (url, domain, title, query, summary, source_kind, provider, origin, interest_key,
                   published_at, published_verified, trust, injection_removed, fetched_at, expires_at, status,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (url, domain, title[:200] or domain, query, (summary or "")[:500], source_kind, provider, origin,
             interest_key, published_at, 1 if published_verified else 0, trust, injection_removed, now, expires, now, now))
        return int(cursor.lastrowid)

    def set_status(self, finding_id: int, status: str) -> dict[str, Any] | None:
        if status not in ("active", "disabled"):
            raise ValueError("status must be active or disabled")
        self.db.execute("UPDATE web_findings SET status = ?, updated_at = ? WHERE id = ?", (status, self.db.now(), finding_id))
        return self.get(finding_id)

    def mark_used(self, ids: Iterable[int]) -> None:
        for finding_id in ids:
            self.db.execute("UPDATE web_findings SET used_count = used_count + 1, used_in_chat_at = ?, updated_at = ? "
                            "WHERE id = ?", (self.db.now(), self.db.now(), finding_id))

    def mark_shared(self, finding_id: int) -> None:
        self.db.execute("UPDATE web_findings SET share_count = share_count + 1, shared_with_user_at = ?, updated_at = ? "
                        "WHERE id = ?", (self.db.now(), self.db.now(), finding_id))

    # -- freshness ---------------------------------------------------------------------
    def is_current(self, finding: dict[str, Any], now: str | None = None) -> bool:
        """Usable at all: active, not expired, domain not disabled."""
        now_dt = parse_time(now or self.db.now())
        return (finding["status"] == "active" and parse_time(finding["expires_at"]) > now_dt
                and not self.domain_disabled(finding["domain"]))

    def is_latest(self, finding: dict[str, Any], freshness_days: float, now: str | None = None) -> bool:
        """May be presented as recent news: current, and published (if known) within freshness_days."""
        if not self.is_current(finding, now):
            return False
        published = parse_time(finding.get("published_at"))
        if published is None:
            fetched = parse_time(finding["fetched_at"])
            return parse_time(now or self.db.now()) - fetched <= timedelta(days=freshness_days)
        return parse_time(now or self.db.now()) - published <= timedelta(days=freshness_days)

    # -- request log ---------------------------------------------------------------------
    def log_request(self, kind: str, origin: str, target: str | None, provider: str | None, status: str,
                    error_code: str | None = None, result_count: int = 0, size: int = 0,
                    latency_ms: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO web_requests (kind, origin, target, provider, status, error_code, result_count, bytes, latency_ms, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, origin, (target or "")[:300], provider, status, error_code, result_count, size, latency_ms, self.db.now()))

    def requests(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.fetch_all("SELECT * FROM web_requests ORDER BY id DESC LIMIT ?", (limit,))

    def count_today(self, kind: str, origin: str | None = None) -> int:
        """Attempts that reached the network / provider today (blocked and over-budget ones do not count)."""
        day = self.db.now()[:10]
        rows = self.db.fetch_all("SELECT origin, status, created_at FROM web_requests WHERE kind = ? AND created_at >= ?",
                                 (kind, day))
        return len([r for r in rows if r["status"] in ("ok", "error") and (origin is None or r["origin"] == origin)])

    def usage_today(self) -> dict[str, Any]:
        day = self.db.now()[:10]
        rows = self.db.fetch_all("SELECT kind, origin, status, COUNT(*) AS n FROM web_requests WHERE created_at >= ? "
                                 "GROUP BY kind, origin, status", (day,))
        return {"date": day, "rows": rows}
