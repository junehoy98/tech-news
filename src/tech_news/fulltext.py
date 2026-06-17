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
from urllib.parse import urljoin

import httpx

from .sources import USER_AGENT, Article

log = logging.getLogger(__name__)

# SEC rejects a generic User-Agent (the package USER_AGENT included) with a 403,
# so EDGAR fetches in this module send a contact UA — the same convention the
# EDGAR ingest adapter uses (see config/sources.toml). The full-text client is
# shared across all sources and carries the package UA, so we override it
# per-request for the EDGAR index + exhibit fetches.
EDGAR_CONTACT_UA = "tech-news-digest (junehoy98@gmail.com)"

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

# Exceptions a single fetch may raise that must degrade to body="" (never crash
# the run). httpx.HTTPError covers timeouts/4xx/5xx/DNS, but a malformed URL —
# from a feed or an EDGAR index href resolved via urljoin — raises off that
# hierarchy: httpx.InvalidURL (e.g. a bad IPv6 literal) and a UnicodeError
# (idna.IDNAError, on an invalid/overlong hostname label).
_FETCH_ERRORS = (httpx.HTTPError, httpx.InvalidURL, UnicodeError)


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

    # Order before capping so the budget goes where it's needed most. A scraped
    # press release or a terse filing ships with NO teaser, so it can't be ranked
    # at all without its body — read those first. Then by source priority. (The
    # new EDGAR/scrape/Federal-Register sources sit at the end of the config, so
    # a naive articles[:max_articles] in fetch order never reached them.) Sorting
    # only changes WHICH items are enriched; body is set on the shared Article
    # objects, so the caller's list is updated regardless of this order.
    ordered = sorted(articles, key=_enrich_priority) if max_articles > 0 else articles
    targets = ordered[:max_articles] if max_articles > 0 else articles

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


# Primary-source kinds (a company's own press release, an SEC filing) that
# routinely ship with NO usable teaser. They literally can't be ranked on their
# headline, so they get first claim on the body-fetch budget — ahead of even
# the generic body-less tier below.
_PRIMARY_TEASERLESS_KINDS = frozenset({"scrape", "edgar"})


def _enrich_priority(article: Article) -> tuple[int, int, int]:
    """Enrichment order: teaserless primary sources, then body-less, then priority.

    Tiered so the body-fetch budget lands where it changes a ranking decision:

    1. Primary sources that can't carry a teaser (scraped press releases, SEC
       filings) — they ship body-less by nature, so without their fetched body
       the ranker sees only a headline. These are read FIRST.
    2. Any other article with an empty/whitespace summary — also starved of the
       prose the ranker scores on, just less reliably so.
    3. Everything else, ordered by source priority.

    `sorted` is stable, so original fetch order is preserved within each tier.
    """
    primary = 0 if article.kind in _PRIMARY_TEASERLESS_KINDS else 1
    starved = 0 if not article.summary.strip() else 1
    return (primary, starved, article.priority)


def _read_one(article: Article, client: httpx.Client) -> str:
    """Fetch one article and return its extracted body, or "" on any failure.

    An EDGAR filing's `url` is an SEC index page, not the prose — route those to
    _read_edgar so we read the attached press release. Everything else is a
    direct page fetch + extract.
    """
    if article.kind == "edgar":
        return _read_edgar(article, client)

    try:
        resp = client.get(article.url)
        resp.raise_for_status()
    except _FETCH_ERRORS as e:
        # Timeout, 403, paywall redirect to login, DNS — and the off-hierarchy
        # httpx.InvalidURL / IDNA UnicodeError a malformed feed URL can raise —
        # all land here and degrade this item to body="".
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


def _read_edgar(article: Article, client: httpx.Client) -> str:
    """Read the prose behind an EDGAR filing — its press-release exhibit, not the index.

    An EDGAR `article.url` points at a filing *index* page (a table of the
    documents in the filing), so extracting it directly yields SEC chrome, not
    the news. Instead we:

      1. Fetch the index (with a contact UA — SEC 403s a generic one).
      2. Find the primary press-release document: prefer an exhibit whose
         type/description names an EX-99 (the earnings/press-release attachment),
         else the main filing .htm document.
      3. Resolve its absolute URL, fetch it, and extract THAT text.

    Defensive: if the exhibit can't be located or fetched, fall back to
    extracting the index page itself; any failure returns "" like every other
    source. Never raises into the pipeline.
    """
    index_html = _fetch_edgar_html(article.url, client)
    if not index_html:
        return ""

    doc_url = _find_edgar_primary_doc(index_html, article.url)
    if doc_url and doc_url != article.url:
        doc_html = _fetch_edgar_html(doc_url, client)
        if doc_html:
            body = _extract(doc_html, doc_url)
            if body:
                log.debug("EDGAR full text %d chars from exhibit %s", len(body), doc_url)
                return body
        # Exhibit was named but couldn't be fetched/extracted — fall through to
        # the index page so we still return *something* over nothing.

    body = _extract(index_html, article.url)
    if body:
        log.debug("EDGAR full text %d chars from index %s", len(body), article.url)
    return body


def _fetch_edgar_html(url: str, client: httpx.Client) -> str:
    """GET an SEC URL with the contact UA; "" on HTTP error or non-HTML body."""
    try:
        resp = client.get(url, headers={"User-Agent": EDGAR_CONTACT_UA})
        resp.raise_for_status()
    except _FETCH_ERRORS as e:
        # HTTP errors plus the off-hierarchy URL/encoding errors (httpx.InvalidURL,
        # idna's UnicodeError) that a malformed index href can raise — all degrade
        # this fetch to "" rather than escaping into enrich().
        log.debug("EDGAR skip %s: %s", url, e)
        return ""

    content_type = resp.headers.get("content-type", "").lower()
    if content_type and not any(hint in content_type for hint in _HTML_CONTENT_HINTS):
        log.debug("EDGAR skip %s: non-HTML content-type %r", url, content_type)
        return ""

    try:
        return resp.text
    except (UnicodeDecodeError, ValueError) as e:
        log.debug("EDGAR skip %s: undecodable body (%s)", url, e)
        return ""


def _find_edgar_primary_doc(index_html: str, index_url: str) -> str:
    """Resolve the URL of a filing's press-release document from its index page.

    The index page tables list each document with a Type column and a link to
    the document. We prefer a row whose type/description contains "EX-99" (the
    press release / earnings attachment), and otherwise fall back to the first
    linked .htm/.html document that isn't the index itself. Returns "" if no
    usable document link is found (caller then reads the index directly).
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(index_html, "html.parser")
    except Exception as e:  # parser shouldn't raise, but never let it escape
        log.debug("EDGAR index parse failed for %s: %s", index_url, e)
        return ""

    ex99_href: str | None = None
    first_doc_href: str | None = None

    for row in soup.find_all("tr"):
        # An anchor to the actual document; index rows without a link are headers.
        link = row.find("a", href=True)
        if link is None:
            continue
        # Modern index pages link the primary document through the inline-XBRL
        # viewer ("/ix?doc=/Archives/.../doc.htm"), which serves a JS shell, not
        # prose. Unwrap to the raw document path so we fetch the real filing.
        href = _unwrap_ix_viewer(link["href"])
        # The row's full text carries the Type column (e.g. "EX-99.1") and the
        # human description, so a substring check catches either placement.
        row_text = row.get_text(" ", strip=True).upper()
        is_html_doc = href.lower().endswith((".htm", ".html"))

        if "EX-99" in row_text and is_html_doc and ex99_href is None:
            ex99_href = href
        if is_html_doc and first_doc_href is None and not _is_index_href(href):
            first_doc_href = href

    chosen = ex99_href or first_doc_href
    if not chosen:
        return ""
    return urljoin(index_url, chosen)


def _is_index_href(href: str) -> bool:
    """True for a link that points back at an index page, not a filing document."""
    low = href.lower()
    return "-index" in low or low.endswith("/index.htm") or low.endswith("/index.html")


def _unwrap_ix_viewer(href: str) -> str:
    """Strip the inline-XBRL viewer wrapper from a document href.

    SEC index pages link a filing's primary (iXBRL-tagged) document through the
    interactive viewer as "/ix?doc=/Archives/.../doc.htm". Fetching that URL
    returns only a "Please enable JavaScript" shell, so we extract the inner
    `doc=` path and fetch the raw document instead. A non-viewer href, or a
    viewer href without a usable inner path, is returned unchanged.
    """
    from urllib.parse import parse_qs, urlsplit

    # Match the viewer regardless of host ("/ix?doc=..." or ".../ix?doc=...").
    split = urlsplit(href)
    if split.path.endswith("/ix") or split.path == "ix":
        doc = parse_qs(split.query).get("doc", [""])[0]
        if doc:
            return doc
    return href


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
