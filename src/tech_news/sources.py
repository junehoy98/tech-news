"""Fetch articles from configured RSS/Atom feeds."""

from __future__ import annotations

import hashlib
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
from dateutil import parser as date_parser

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

UTC = timezone.utc

log = logging.getLogger(__name__)

USER_AGENT = "tech-news-digest/0.1 (+https://github.com/junehoy98/tech-news)"
FETCH_TIMEOUT = 20.0

# Default recency window applied before ranking. The ranker scores every new
# article in a single LLM call, so unbounded input is both a cost and a
# correctness risk (it can overflow the output cap). A week covers weekend
# gaps and the GitHub scheduler's lag while keeping the batch small.
DEFAULT_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    category: str
    priority: int


@dataclass
class Article:
    url: str
    title: str
    summary: str
    published: datetime
    source_name: str
    category: str
    priority: int

    @property
    def fingerprint(self) -> str:
        """Stable ID used for dedupe — URL is usually canonical enough."""
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]


def load_sources(config_path: Path) -> list[Source]:
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    return [Source(**entry) for entry in data["sources"]]


def fetch_source(source: Source, client: httpx.Client) -> list[Article]:
    """Fetch one feed; returns [] on any error rather than crashing the run."""
    try:
        resp = client.get(source.url, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Skipping %s: %s", source.name, e)
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        log.warning("Skipping %s: malformed feed (%s)", source.name, parsed.bozo_exception)
        return []

    articles = []
    for entry in parsed.entries:
        url = entry.get("link")
        title = entry.get("title", "").strip()
        if not url or not title:
            continue

        summary = entry.get("summary", "") or entry.get("description", "")
        summary = _strip_html(summary)[:1000]

        published = _parse_date(entry)

        articles.append(
            Article(
                url=url,
                title=title,
                summary=summary,
                published=published,
                source_name=source.name,
                category=source.category,
                priority=source.priority,
            )
        )
    return articles


def fetch_all(sources: list[Source]) -> list[Article]:
    """Fetch every source sequentially; concurrency is overkill at ~10 feeds."""
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, timeout=FETCH_TIMEOUT) as client:
        articles = []
        for src in sources:
            new = fetch_source(src, client)
            log.info("Fetched %d items from %s", len(new), src.name)
            articles.extend(new)
    return articles


def filter_recent(
    articles: list[Article],
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> list[Article]:
    """Drop articles older than `max_age_days` by published date.

    Bounds the ranking batch: without this, a feed that publishes a long
    archive (or the first run against an empty dedupe DB) would push hundreds
    of stale items into the single-shot ranker. Pass max_age_days <= 0 to
    disable. Items with no parseable date default to `now` in _parse_date, so
    they're never dropped here.
    """
    if max_age_days <= 0:
        return articles
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=max_age_days)
    return [a for a in articles if a.published >= cutoff]


def _parse_date(entry: feedparser.FeedParserDict) -> datetime:
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                dt = date_parser.parse(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except (ValueError, TypeError):
                continue
    return datetime.now(UTC)


def _strip_html(text: str) -> str:
    """Lightweight HTML strip — full bs4 parse is wasteful for plain summaries."""
    if not text or "<" not in text:
        return text.strip()
    from bs4 import BeautifulSoup

    return BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()
