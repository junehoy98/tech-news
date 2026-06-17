"""Non-RSS ingest adapters, registered into sources.INGESTORS.

The base ingest layer (sources.py) only ships the plain-RSS adapter. This
module is the home for everything else — currently the SEC EDGAR filings
poller, the Federal Register API, and an HTML-scrape adapter for company
newsrooms that dropped RSS — so the RSS path stays small and these
specialized fetchers live together.

Wiring: each adapter registers itself into sources.INGESTORS at import time
(see the bottom of this file). sources.py imports this module for that side
effect, so any code that imports `tech_news.sources` gets the full registry
without naming individual adapters. The import sits at the *end* of sources.py,
after INGESTORS is defined, which keeps the cycle harmless — by the time this
module runs, every name it needs from sources already exists.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .sources import (
    INGESTORS,
    USER_AGENT,
    UTC,
    Article,
    Source,
    _parse_date,
    _strip_html,
    log,
)

# Per-company filings feed. SEC requires a contact User-Agent (a generic one
# gets a 403), which we pass via source.user_agent; output=atom gives us a feed
# feedparser reads like any other. CIK accepts a ticker symbol directly.
EDGAR_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK={ticker}&type={form}&count={count}&output=atom"
)

# How many recent filings to request per filer. filter_recent trims by date
# downstream; this just caps the feed so we don't pull a long back-catalog.
EDGAR_DEFAULT_COUNT = 10


def fetch_edgar(source: Source, client: httpx.Client) -> list[Article]:
    """Fetch recent SEC filings for each filer in source.options.

    options shape:
        {"filings": [{"ticker": "KLAC", "form": "8-K"},
                     {"ticker": "ASML", "form": "6-K"}],
         "count": 10}  # optional, defaults to EDGAR_DEFAULT_COUNT

    One Atom feed is fetched per filer (US 8-K, foreign 6-K, etc.). A per-filer
    error or 403 is logged and skipped so one bad ticker never sinks the run.
    """
    headers = {"User-Agent": source.user_agent or USER_AGENT}
    count = source.options.get("count", EDGAR_DEFAULT_COUNT)

    articles: list[Article] = []
    for filing in source.options.get("filings", []):
        ticker = filing.get("ticker")
        form = filing.get("form")
        if not ticker or not form:
            log.warning(
                "%s: skipping EDGAR entry with missing ticker/form: %r", source.name, filing
            )
            continue
        articles.extend(_fetch_edgar_one(source, client, headers, ticker, form, count))
    return articles


def _fetch_edgar_one(
    source: Source,
    client: httpx.Client,
    headers: dict[str, str],
    ticker: str,
    form: str,
    count: int,
) -> list[Article]:
    """Fetch and parse one filer's Atom feed; [] on any error rather than crash."""
    url = EDGAR_FEED_URL.format(ticker=ticker, form=form, count=count)
    try:
        resp = client.get(url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        # 403 here almost always means the contact User-Agent wasn't accepted.
        log.warning("Skipping EDGAR %s (%s): %s", ticker, form, e)
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        log.warning(
            "Skipping EDGAR %s (%s): malformed feed (%s)",
            ticker, form, parsed.bozo_exception,
        )
        return []

    # The feed title carries the issuer's conformed name, e.g.
    # "KLA CORP  (0000319201)" — trim the trailing CIK for a clean label.
    filer = parsed.feed.get("title", ticker).split("  (")[0].strip()

    articles = []
    for entry in parsed.entries:
        url = entry.get("link")
        if not url:
            continue

        title = _edgar_title(filer, form, entry)
        summary = _strip_html(entry.get("summary", ""))[:1000]
        # EDGAR stamps an offset-aware <updated>; normalize to UTC like the
        # rest of the pipeline expects.
        published = _parse_date(entry).astimezone(UTC)

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


def _edgar_title(filer: str, form: str, entry: feedparser.FeedParserDict) -> str:
    """Build a readable "<filer> — <form>: <description>" headline.

    The entry title is "8-K  - Current report"; we keep the human description
    and prefix it with the filer and form so the ranker sees who filed what.
    """
    entry_title = entry.get("title", "").strip()
    # Strip the leading form code and separator that EDGAR repeats, leaving the
    # description ("Current report", "Report of foreign issuer [...]").
    desc = entry_title.split(" - ", 1)[-1].strip() if " - " in entry_title else entry_title
    if desc and desc.lower() != form.lower():
        return f"{filer} — {form}: {desc}"
    return f"{filer} — {form}"


# Federal Register API: the official daily journal of US rules and notices.
# We poll one agency's recent documents (BIS export controls are the use case)
# filtered by a search term. The JSON endpoint works with the default UA — no
# contact header needed, unlike EDGAR.
FEDERAL_REGISTER_URL = "https://www.federalregister.gov/api/v1/articles.json"

# How many recent documents to request. filter_recent trims by date downstream;
# this just caps the page so we don't pull a long back-catalog.
FEDERAL_REGISTER_DEFAULT_PER_PAGE = 20


def fetch_federal_register(source: Source, client: httpx.Client) -> list[Article]:
    """Fetch recent Federal Register documents for the agencies in source.options.

    options shape:
        {"agencies": ["industry-and-security-bureau"],  # agency slugs
         "term": "semiconductor",   # optional full-text filter
         "per_page": 20,            # optional, defaults to the constant above
         "order": "newest"}         # optional, defaults to "newest"

    The API nests filters under conditions[...]; we build the param list by hand
    so a multi-agency request repeats conditions[agencies][] correctly. Any
    HTTP/JSON error is logged and yields [] so a bad response never sinks the run.
    """
    headers = {"User-Agent": source.user_agent or USER_AGENT}

    # httpx repeats a key for each list item, which is exactly the
    # conditions[agencies][] shape the API wants — so build a flat (key, value)
    # list rather than a dict.
    params: list[tuple[str, str | int]] = []
    for agency in source.options.get("agencies", []):
        params.append(("conditions[agencies][]", agency))
    term = source.options.get("term")
    if term:
        params.append(("conditions[term]", term))
    params.append(("per_page", source.options.get("per_page", FEDERAL_REGISTER_DEFAULT_PER_PAGE)))
    params.append(("order", source.options.get("order", "newest")))

    try:
        resp = client.get(FEDERAL_REGISTER_URL, params=params, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as e:
        log.warning("Skipping %s: %s", source.name, e)
        return []
    except ValueError as e:
        # resp.json() raises ValueError on a non-JSON body (e.g. an error page).
        log.warning("Skipping %s: malformed JSON (%s)", source.name, e)
        return []

    articles = []
    for result in payload.get("results", []):
        url = result.get("html_url")
        title = (result.get("title") or "").strip()
        if not url or not title:
            continue

        summary = _strip_html(result.get("abstract") or "")[:1000]
        published = _federal_register_date(result.get("publication_date"))

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


def _federal_register_date(raw: str | None) -> datetime:
    """Parse the date-only publication_date ("2026-01-15") to tz-aware UTC midnight.

    The API stamps a bare calendar date with no time or zone, so we anchor it to
    midnight UTC to satisfy the pipeline's tz-aware contract. Missing/unparseable
    dates fall back to now() like _parse_date does, so the item isn't dropped.
    """
    if raw:
        try:
            dt = date_parser.parse(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except (ValueError, TypeError):
            pass
    return datetime.now(UTC)


# HTML-scrape adapter: for company newsrooms that dropped RSS. Several semi
# equipment makers (ASML, KLA, Onto Innovation) and SEMI.org publish press
# releases only as HTML now, so we parse the listing page with CSS selectors
# pulled from source.options. This is config-driven on purpose: when a site
# re-skins its newsroom (which it will), you update the selectors in
# config/sources.toml — NOT this code.
#
# !!! THE SELECTORS BELOW LIVE IN config/sources.toml AND ARE BRITTLE. !!!
# They were derived by fetching each live page once with a browser-like UA and
# inspecting the markup (June 2026). When a page stops yielding items, refetch
# it (curl/httpx with source.user_agent) and re-derive the {item,title,link,
# date} selectors. The adapter NEVER throws on a selector miss — it logs a
# warning and returns [] — so a layout drift degrades to "no items from this
# source," never a crashed run.

# Date formats vary by site (ASML: "May 16, 2026"; Onto: "5/19/26"; KLA stamps
# a machine <time datetime="..."> we prefer). dateutil handles all of them, so
# we lean on _parse_date's fallback-to-now behavior for anything unparseable.


def fetch_scrape(source: Source, client: httpx.Client) -> list[Article]:
    """Scrape a newsroom listing page into Articles using CSS selectors.

    options shape (all selectors are CSS, matched with BeautifulSoup.select):
        {"page_url": "https://www.asml.com/en/news/press-releases",
         "base_url": "https://www.asml.com",  # resolves relative links
         "item":  ".related-content-card--story",  # one per article
         "title": ".related-content-card-title",   # within item; "" -> item text
         "link":  "",   # within item; "" -> use the item itself if it's an <a>
         "date":  ".related-content-card-subtitle"}  # within item; optional

    Defensive by design: a fetch error, a missing item selector, or zero matched
    items each logs a warning and returns [] rather than raising. A per-item
    parse problem skips that item only.
    """
    opts = source.options
    page_url = opts.get("page_url") or source.url
    if not page_url:
        log.warning("Skipping %s: no page_url/url for scrape source", source.name)
        return []

    headers = {"User-Agent": source.user_agent or USER_AGENT}
    try:
        resp = client.get(page_url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        # SEMI.org sits behind a Cloudflare JS challenge that a plain client
        # can't pass, so it 403s here and degrades to []. See follow-ups.
        log.warning("Skipping %s: %s", source.name, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    item_sel = opts.get("item")
    if not item_sel:
        log.warning("Skipping %s: scrape source has no 'item' selector", source.name)
        return []

    items = soup.select(item_sel)
    if not items:
        # Layout drifted (or the page was a challenge/error body). Don't crash —
        # the next run after the selectors are fixed will pick items back up.
        log.warning(
            "%s: item selector %r matched nothing — layout likely changed",
            source.name, item_sel,
        )
        return []

    base_url = opts.get("base_url", "")
    title_sel = opts.get("title", "")
    link_sel = opts.get("link", "")
    date_sel = opts.get("date", "")

    articles: list[Article] = []
    for item in items:
        article = _scrape_one(
            source, item, base_url, title_sel, link_sel, date_sel
        )
        if article is not None:
            articles.append(article)
    return articles


def _scrape_one(
    source: Source,
    item,
    base_url: str,
    title_sel: str,
    link_sel: str,
    date_sel: str,
) -> Article | None:
    """Turn one matched list element into an Article, or None if unusable.

    `item` is a bs4 Tag. Selectors are matched *within* it. An empty selector
    means "use the item element itself" (handy when the item is the <a> that
    carries both the title text and the href, as on ASML).
    """
    # Link: the configured sub-element's href, or the item's own href.
    link_node = item.select_one(link_sel) if link_sel else item
    href = link_node.get("href") if link_node is not None else None
    if not href:
        return None
    # Resolve a relative link against base_url; absolute hrefs pass through.
    url = urljoin(base_url, href) if base_url else href

    # Title: the configured sub-element's text, or the whole item's text.
    title_node = item.select_one(title_sel) if title_sel else item
    title = title_node.get_text(" ", strip=True) if title_node is not None else ""
    title = _strip_html(title)
    if not title:
        return None

    published = _scrape_date(item, date_sel)

    return Article(
        url=url,
        title=title,
        # Listing pages rarely carry a usable blurb; the ranker scores on the
        # headline when the summary is empty (same as paywalled-feed stubs).
        summary="",
        published=published,
        source_name=source.name,
        category=source.category,
        priority=source.priority,
    )


def _scrape_date(item, date_sel: str) -> datetime:
    """Parse a date out of the item, defaulting to now() like _parse_date.

    Prefers a machine-readable <time datetime="..."> attribute (KLA stamps one)
    over visible text ("May 16, 2026", "5/19/26"). Anything unparseable or
    absent falls back to now() so the item still appears in the digest.
    """
    if date_sel:
        date_node = item.select_one(date_sel)
        if date_node is not None:
            # <time datetime="2026-05-13T20:00:00+00:00"> beats the human label.
            raw = date_node.get("datetime") or date_node.get_text(" ", strip=True)
            if raw:
                try:
                    dt = date_parser.parse(raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    return dt.astimezone(UTC)
                except (ValueError, TypeError, OverflowError):
                    pass
    return datetime.now(UTC)


# Side-effect registration: makes EDGAR, the Federal Register, and the HTML
# scraper available to fetch_source via Source.kind. Importing tech_news.sources
# pulls this module in (see sources.py).
INGESTORS["edgar"] = fetch_edgar
INGESTORS["federal_register"] = fetch_federal_register
INGESTORS["scrape"] = fetch_scrape
