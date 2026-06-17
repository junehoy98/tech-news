"""Fetch articles from configured sources via pluggable ingest adapters.

Most sources are plain RSS/Atom feeds, but the ingest layer dispatches on each
source's `kind` so non-RSS adapters (e.g. an SEC EDGAR poller) can be added by
registering a fetcher in INGESTORS — no special-casing the pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
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


# Not frozen: `options` is a mutable dict (an unhashable default would make a
# frozen dataclass unhashable anyway), and nothing in the pipeline hashes a
# Source or uses one as a set/dict key.
@dataclass
class Source:
    name: str
    url: str
    category: str
    priority: int
    # Selects the ingest adapter in INGESTORS; "rss" covers every current feed.
    kind: str = "rss"
    # Per-source User-Agent override (some endpoints, e.g. SEC EDGAR, require a
    # contact UA). Falls back to the package-wide USER_AGENT when unset.
    user_agent: str | None = None
    # Adapter-specific parameters (e.g. an EDGAR CIK list); ignored by RSS.
    options: dict = field(default_factory=dict)


@dataclass
class Article:
    url: str
    title: str
    summary: str
    published: datetime
    source_name: str
    category: str
    priority: int
    # Main article body text, populated by fulltext.enrich() after fetch/dedupe.
    # Empty when full-text reading is disabled, the fetch failed, or the page
    # yielded no extractable prose (paywall, PDF, 403). Consumers prefer it over
    # `summary` when present but always fall back to the teaser.
    body: str = ""

    @property
    def fingerprint(self) -> str:
        """Stable ID used for dedupe — URL is usually canonical enough."""
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]


# TOML keys we accept on a [[sources]] table. Anything else is ignored so an
# unknown adapter's stray field can't crash load; kind/user_agent/options are
# optional and fall back to the Source defaults when absent.
_SOURCE_FIELDS = ("name", "url", "category", "priority", "kind", "user_agent", "options")


def load_sources(config_path: Path) -> list[Source]:
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    return [
        Source(**{k: v for k, v in entry.items() if k in _SOURCE_FIELDS})
        for entry in data["sources"]
    ]


def _fetch_rss(source: Source, client: httpx.Client) -> list[Article]:
    """Fetch one RSS/Atom feed; returns [] on any error rather than crashing the run."""
    headers = {"User-Agent": source.user_agent or USER_AGENT}
    try:
        resp = client.get(source.url, headers=headers, follow_redirects=True)
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


# Ingest-adapter registry: maps Source.kind -> a fetcher (source, client) -> list.
# Later non-RSS adapters register here instead of editing fetch_source.
INGESTORS: dict[str, Callable[[Source, httpx.Client], list[Article]]] = {
    "rss": _fetch_rss,
}


def fetch_source(source: Source, client: httpx.Client) -> list[Article]:
    """Dispatch to the ingestor for source.kind; unknown kinds log + skip."""
    ingestor = INGESTORS.get(source.kind)
    if ingestor is None:
        log.warning(
            "Skipping %s: unknown source kind %r (known: %s)",
            source.name, source.kind, ", ".join(sorted(INGESTORS)),
        )
        return []
    return ingestor(source, client)


def fetch_all(sources: list[Source]) -> list[Article]:
    """Fetch every source sequentially; concurrency is overkill at ~10 feeds."""
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, timeout=FETCH_TIMEOUT) as client:
        articles = []
        for src in sources:
            # Belt-and-suspenders isolation: each ingestor already catches its
            # expected HTTP/parse errors, but the non-RSS adapters have many more
            # failure surfaces (a typo'd config CSS selector raises
            # SelectorSyntaxError, a malformed options shape raises AttributeError,
            # a bad URL raises httpx.InvalidURL — none of which are httpx.HTTPError).
            # Any such surprise must skip just this source, never sink the run and
            # lose every source after it.
            try:
                new = fetch_source(src, client)
            except Exception as e:  # noqa: BLE001 — one bad source can't kill the run
                log.warning("Skipping %s: unexpected fetch error: %s", src.name, e)
                continue
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
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
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


# Pull in the non-RSS adapters for their side-effect registration into
# INGESTORS (e.g. adapters.fetch_edgar -> INGESTORS["edgar"]). This sits at the
# bottom, after INGESTORS and the shared helpers exist, so the import cycle with
# adapters.py resolves cleanly. noqa: E402/F401 — late, side-effect-only import.
from . import adapters  # noqa: E402, F401
