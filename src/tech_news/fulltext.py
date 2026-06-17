"""Fetch each article's page and extract its main body text.

The RSS/Atom teaser an article ships with is often a one-line blurb (or empty,
for the scrape/EDGAR adapters). That starves both LLM passes — the ranker
scores on a headline alone and the synthesizer paraphrases a stub. This module
fetches the article URL and pulls out the readable body so downstream prompts
work from the actual reporting.

Extraction is best-effort and degrades gracefully: trafilatura does the heavy
lifting, a light BeautifulSoup heuristic backs it up, and ANY per-URL failure
(timeout, paywall, 403, PDF, non-HTML) leaves body="" so the existing summary
still carries the item. Nothing here ever raises into the pipeline.
"""

from __future__ import annotations

import logging

import httpx

from .sources import USER_AGENT, Article

log = logging.getLogger(__name__)

# Cap on stored body length. ~4000 chars (~1K tokens) is plenty of lede +
# nut-graf for the ranker and synthesizer without blowing up LLM input across a
# whole batch; the rest of a long feature adds cost, not signal.
MAX_BODY_CHARS = 4000

# How many of the (already recency-trimmed, deduped) new articles to enrich.
# Bounds the number of outbound fetches per run; the ranker's batch math and
# the synthesis candidate pool are the real consumers, so there's no point
# reading the long tail that won't survive scoring.
DEFAULT_MAX_ARTICLES = 60

# Per-request timeout. Newsrooms can be slow; a stuck fetch shouldn't stall the
# run, so this is short and a timeout simply degrades that item to body="".
DEFAULT_TIMEOUT = 15.0

# Content types we won't even try to extract prose from — PDFs, images, feeds.
# We only read text/html (and the occasional XHTML). Anything else degrades to
# body="" without spending a parse.
_HTML_CONTENT_HINTS = ("text/html", "application/xhtml")


def enrich(
    articles: list[Article],
    *,
    max_articles: int = DEFAULT_MAX_ARTICLES,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> int:
    """Populate `article.body` in place for up to `max_articles` items.

    Fetches each article's URL sequentially (polite: one shared client, the
    package USER_AGENT, a short timeout) and sets `body` to the extracted main
    text, capped at MAX_BODY_CHARS. Articles past `max_articles`, and any whose
    fetch/extract fails, keep body="" and rely on their existing summary.

    Returns the number of articles that got a non-empty body, for logging.
    Never raises — a bad page degrades that one item, not the run.
    """
    if not articles:
        return 0

    targets = articles[:max_articles] if max_articles > 0 else articles

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
    try:
        fetched = 0
        for article in targets:
            body = _read_one(article, client)
            if body:
                article.body = body
                fetched += 1
        return fetched
    finally:
        if owns_client:
            client.close()


def _read_one(article: Article, client: httpx.Client) -> str:
    """Fetch one article URL and return its extracted body, or "" on any failure."""
    try:
        resp = client.get(article.url)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        # Timeout, 403, paywall redirect to login, DNS — all land here.
        log.debug("Full text skip %s: %s", article.url, e)
        return ""

    # Skip non-HTML bodies (PDF, images, raw feeds) — there's no prose to mine,
    # and handing a PDF to an HTML parser just wastes time.
    content_type = resp.headers.get("content-type", "").lower()
    if content_type and not any(hint in content_type for hint in _HTML_CONTENT_HINTS):
        log.debug("Full text skip %s: non-HTML content-type %r", article.url, content_type)
        return ""

    try:
        html = resp.text
    except (UnicodeDecodeError, ValueError) as e:
        log.debug("Full text skip %s: undecodable body (%s)", article.url, e)
        return ""

    body = _extract(html, article.url)
    if body:
        log.debug("Full text %d chars from %s", len(body), article.url)
    return body


def _extract(html: str, url: str) -> str:
    """Extract main body text from an HTML string, capped at MAX_BODY_CHARS.

    Tries trafilatura first (boilerplate-aware), then falls back to a light
    BeautifulSoup main-content heuristic. Returns "" if neither finds prose.
    """
    text = _extract_trafilatura(html, url)
    if not text:
        text = _extract_bs4(html)
    if not text:
        return ""
    # Collapse runaway whitespace and cap. Truncate on a word boundary when one
    # is near the cap so we don't slice a word mid-token.
    text = " ".join(text.split())
    if len(text) > MAX_BODY_CHARS:
        cut = text.rfind(" ", 0, MAX_BODY_CHARS)
        text = text[: cut if cut > MAX_BODY_CHARS - 200 else MAX_BODY_CHARS].rstrip()
    return text


def _extract_trafilatura(html: str, url: str) -> str:
    """Run trafilatura's extractor; "" if it's unavailable or finds nothing."""
    try:
        import trafilatura
    except ImportError:
        # Dependency declared in pyproject, but don't hard-crash the run if a
        # stripped install is missing it — the bs4 fallback still works.
        log.debug("trafilatura not installed; using bs4 fallback")
        return ""

    try:
        # favor_precision trims marginal boilerplate; comments off avoids the
        # duplicated comment-section text trafilatura otherwise appends.
        text = trafilatura.extract(
            html,
            url=url,
            favor_precision=True,
            include_comments=False,
            include_tables=False,
        )
    except Exception as e:  # trafilatura/lxml can raise on pathological markup
        log.debug("trafilatura extract failed for %s: %s", url, e)
        return ""
    return (text or "").strip()


# Tags whose text is navigation/boilerplate, not article prose. Dropped before
# the heuristic reads the main region so menus and scripts don't pollute it.
_BS4_DROP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript")


def _extract_bs4(html: str) -> str:
    """Fallback: grab the densest main-content region with BeautifulSoup.

    Looks for the obvious semantic containers (<article>, <main>, role=main)
    and falls back to <body>, after stripping nav/script/style chrome. Crude
    next to trafilatura but enough to recover prose from simple pages.
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:  # parser shouldn't raise, but never let it escape
        log.debug("bs4 parse failed: %s", e)
        return ""

    for tag in soup(list(_BS4_DROP_TAGS)):
        tag.decompose()

    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )
    return container.get_text(" ", strip=True)
