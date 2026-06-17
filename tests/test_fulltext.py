"""Offline tests for fulltext.enrich() — the fetch is always mocked.

These never touch the network: a MagicMock stands in for httpx.Client, returning
canned HTML/headers, so extraction and the graceful-fallback paths are exercised
deterministically.
"""

from pathlib import Path
from unittest.mock import MagicMock

import httpx

from tech_news import fulltext
from tech_news.fulltext import (
    EDGAR_CONTACT_UA,
    MAX_BODY_CHARS,
    _enrich_priority,
    _extract,
    _extract_bs4,
    _find_edgar_primary_doc,
    _read_edgar,
    _unwrap_ix_viewer,
    enrich,
)

FIXTURES = Path(__file__).parent / "fixtures"

# A realistic-enough article page: an <article> with real prose, plus nav/footer
# chrome that a good extractor drops.
ARTICLE_HTML = """
<html><head><title>ASML ships High-NA</title></head>
<body>
  <nav>Home | News | Contact</nav>
  <header>Site banner</header>
  <article>
    <h1>ASML ships its first High-NA EUV system</h1>
    <p>ASML has shipped the first High-NA extreme ultraviolet lithography
    system to a leading logic customer, the company said on Tuesday. The tool
    is expected to enable smaller feature sizes at advanced nodes and marks a
    milestone for the Dutch equipment maker after years of development.</p>
    <p>Installation and qualification will take several months before the
    system enters volume production, executives noted on the earnings call.</p>
  </article>
  <footer>Copyright 2026. All rights reserved.</footer>
  <script>var tracking = true;</script>
</body></html>
"""


def _html_client(html: str, content_type: str = "text/html; charset=utf-8") -> MagicMock:
    """An httpx.Client stand-in whose get() returns `html` with the given type."""
    resp = MagicMock()
    resp.text = html
    resp.headers = {"content-type": content_type}
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    return client


def test_enrich_populates_body_from_html(article_factory):
    a = article_factory(url="https://example.com/asml-high-na", summary="teaser")
    client = _html_client(ARTICLE_HTML)

    fetched = enrich([a], client=client)

    assert fetched == 1
    assert "first High-NA extreme ultraviolet lithography" in a.body
    # Boilerplate chrome is dropped by the extractor.
    assert "All rights reserved" not in a.body
    assert "var tracking" not in a.body
    # The original teaser is left untouched on the article.
    assert a.summary == "teaser"


def test_enrich_returns_count_of_bodies_read(article_factory):
    good = article_factory(url="https://example.com/good")
    # Second URL 403s, so it contributes no body.
    bad = article_factory(url="https://example.com/bad")

    good_resp = MagicMock()
    good_resp.text = ARTICLE_HTML
    good_resp.headers = {"content-type": "text/html"}
    good_resp.raise_for_status.return_value = None

    bad_resp = MagicMock()
    bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403 Forbidden", request=MagicMock(), response=MagicMock()
    )

    client = MagicMock()
    client.get.side_effect = [good_resp, bad_resp]

    fetched = enrich([good, bad], client=client)

    assert fetched == 1
    assert good.body
    assert bad.body == ""


def test_enrich_uses_package_user_agent_when_owning_client(article_factory, mocker):
    # When enrich() creates its own client, it must send the package USER_AGENT.
    a = article_factory(url="https://example.com/a")

    fake_client = _html_client(ARTICLE_HTML)
    ctor = mocker.patch("tech_news.fulltext.httpx.Client", return_value=fake_client)

    enrich([a])

    _, kwargs = ctor.call_args
    assert kwargs["headers"]["User-Agent"] == fulltext.USER_AGENT
    assert kwargs["follow_redirects"] is True
    # A self-created client is closed when done.
    fake_client.close.assert_called_once()


def test_enrich_http_error_degrades_to_empty_body(article_factory):
    a = article_factory(url="https://example.com/timeout", summary="fallback teaser")

    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.ConnectTimeout("timed out")
    client = MagicMock()
    client.get.return_value = resp

    fetched = enrich([a], client=client)

    assert fetched == 0
    assert a.body == ""
    # The summary is the fallback, untouched.
    assert a.summary == "fallback teaser"


def test_enrich_skips_non_html_content_type(article_factory):
    # A PDF (or image) link must not be parsed; body stays empty.
    a = article_factory(url="https://example.com/filing.pdf")
    client = _html_client("%PDF-1.7 binary junk", content_type="application/pdf")

    fetched = enrich([a], client=client)

    assert fetched == 0
    assert a.body == ""


def test_enrich_empty_extraction_leaves_body_empty(article_factory):
    # A page with no extractable prose (empty body) yields no body.
    a = article_factory(url="https://example.com/empty")
    client = _html_client("<html><head><title>Nothing</title></head><body></body></html>")

    fetched = enrich([a], client=client)

    assert fetched == 0
    assert a.body == ""


def test_enrich_respects_max_articles(article_factory):
    arts = [article_factory(url=f"https://example.com/{i}") for i in range(5)]
    client = _html_client(ARTICLE_HTML)

    enrich(arts, max_articles=2, client=client)

    # Only the first two are fetched; the rest are left with empty bodies.
    assert client.get.call_count == 2
    assert all(a.body for a in arts[:2])
    assert all(a.body == "" for a in arts[2:])


def test_enrich_empty_input_returns_zero():
    assert enrich([]) == 0


def test_enrich_caps_body_length(article_factory):
    # A very long body is truncated to MAX_BODY_CHARS.
    long_para = "word " * 5000  # ~25K chars, well past the cap
    html = f"<html><body><article><p>{long_para}</p></article></body></html>"
    a = article_factory(url="https://example.com/long")
    client = _html_client(html)

    enrich([a], client=client)

    assert 0 < len(a.body) <= MAX_BODY_CHARS


def test_extract_falls_back_to_bs4_when_trafilatura_empty(mocker, article_factory):
    # Force trafilatura to return nothing; the bs4 heuristic must recover prose.
    mocker.patch("tech_news.fulltext._extract_trafilatura", return_value="")

    body = _extract(ARTICLE_HTML, "https://example.com/a")

    assert "first High-NA extreme ultraviolet lithography" in body
    # bs4 fallback also drops script/footer chrome.
    assert "var tracking" not in body


def test_extract_bs4_drops_boilerplate_tags():
    html = (
        "<html><body><nav>navlinks</nav>"
        "<article><p>The real article text lives here.</p></article>"
        "<footer>footer junk</footer>"
        "<script>code()</script></body></html>"
    )
    body = _extract_bs4(html)
    assert "The real article text lives here." in body
    assert "navlinks" not in body
    assert "footer junk" not in body
    assert "code()" not in body


def test_extract_returns_empty_on_no_prose():
    assert _extract("<html><body></body></html>", "https://example.com/x") == ""


# --------------------------------------------------------------------------- #
# Enrichment ordering (_enrich_priority)                                       #
# --------------------------------------------------------------------------- #


def test_enrich_priority_reads_primary_teaserless_kinds_first(article_factory):
    """scrape/edgar sources (can't be ranked on a headline) sort ahead of all else.

    A scraped release and a filing — even at WORSE source priority and even when
    a feed item is itself body-less — must come first, because they have no
    teaser at all to rank on.
    """
    rss_starved = article_factory(url="rss-starved", summary="", priority=0, kind="rss")
    rss_teaser = article_factory(url="rss-teaser", summary="blurb", priority=0, kind="rss")
    scrape = article_factory(url="scrape", summary="", priority=9, kind="scrape")
    edgar = article_factory(url="edgar", summary="", priority=9, kind="edgar")

    ordered = sorted([rss_teaser, rss_starved, edgar, scrape], key=_enrich_priority)

    # Both primary teaserless kinds lead (stable, so in their input order: edgar
    # then scrape), then the body-less RSS, then the teaser-bearing one.
    assert [a.url for a in ordered] == ["edgar", "scrape", "rss-starved", "rss-teaser"]


def test_enrich_priority_is_stable_within_a_tier(article_factory):
    """Within the same (primary, starved, priority) tier, fetch order is preserved."""
    a = article_factory(url="a", summary="", priority=1, kind="scrape")
    b = article_factory(url="b", summary="", priority=1, kind="scrape")
    c = article_factory(url="c", summary="", priority=1, kind="edgar")

    ordered = sorted([a, b, c], key=_enrich_priority)

    # a, b, c are all primary/teaserless/priority-1 → original order kept.
    assert [x.url for x in ordered] == ["a", "b", "c"]


def test_enrich_priority_falls_back_to_starved_then_priority(article_factory):
    """With no primary-kind items, the prior (starved, priority) ordering holds."""
    teaser_hi = article_factory(url="teaser-hi", summary="x", priority=0, kind="rss")
    teaser_lo = article_factory(url="teaser-lo", summary="x", priority=5, kind="rss")
    starved = article_factory(url="starved", summary="   ", priority=5, kind="rss")

    ordered = sorted([teaser_lo, teaser_hi, starved], key=_enrich_priority)

    # Body-less first, then teaser-bearing by ascending priority number.
    assert [a.url for a in ordered] == ["starved", "teaser-hi", "teaser-lo"]


def test_enrich_reads_primary_source_before_a_capped_feed(article_factory):
    """End-to-end: with a tight budget, the body-less filing wins the one slot."""
    # A teaser-bearing RSS item listed first, and a teaserless EDGAR filing last.
    feed = article_factory(url="https://example.com/feed", summary="has a teaser", kind="rss")
    filing = article_factory(url="https://example.com/filing", summary="", kind="edgar")
    client = _html_client(ARTICLE_HTML)

    enrich([feed, filing], max_articles=1, client=client)

    # The single fetch went to the filing, not the already-rankable feed item.
    assert filing.body
    assert feed.body == ""
    assert client.get.call_count == 1


# --------------------------------------------------------------------------- #
# EDGAR-aware extraction (_read_edgar)                                          #
#                                                                             #
# An EDGAR article.url is a filing INDEX page; _read_edgar must hop to the     #
# attached press release. These run fully offline against saved fixtures.      #
# --------------------------------------------------------------------------- #

INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/319201/"
    "000031920126000014/0000319201-26-000014-index.htm"
)


def _url_client(pages: dict[str, str], content_type: str = "text/html; charset=utf-8"):
    """An httpx.Client stand-in that returns canned HTML keyed by requested URL.

    A URL not in `pages` raises a 404 HTTPStatusError, modelling an exhibit link
    that can't be fetched. `content_type` is returned for every served page.
    """

    def _get(url, *args, **kwargs):
        resp = MagicMock()
        if url not in pages:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "404 Not Found", request=MagicMock(), response=MagicMock()
            )
            return resp
        resp.text = pages[url]
        resp.headers = {"content-type": content_type}
        resp.raise_for_status.return_value = None
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    return client


def test_find_edgar_primary_doc_prefers_ex99_exhibit():
    index_html = (FIXTURES / "edgar_index_klac_8k.html").read_text(encoding="utf-8")

    doc_url = _find_edgar_primary_doc(index_html, INDEX_URL)

    # The EX-99.1 press release wins over the main 8-K .htm and the graphic.
    assert doc_url == (
        "https://www.sec.gov/Archives/edgar/data/319201/"
        "000031920126000014/d12345dex991.htm"
    )


def test_find_edgar_primary_doc_falls_back_to_main_htm():
    index_html = (FIXTURES / "edgar_index_no_exhibit.html").read_text(encoding="utf-8")

    doc_url = _find_edgar_primary_doc(index_html, INDEX_URL)

    # No EX-99 present, so the main filing .htm is chosen — never the -index link.
    assert doc_url == (
        "https://www.sec.gov/Archives/edgar/data/319201/"
        "000031920126000014/asml6k.htm"
    )
    assert "-index" not in doc_url


def test_read_edgar_extracts_exhibit_text_not_index_chrome(article_factory):
    index_html = (FIXTURES / "edgar_index_klac_8k.html").read_text(encoding="utf-8")
    ex991_html = (FIXTURES / "edgar_ex991_klac.html").read_text(encoding="utf-8")
    exhibit_url = (
        "https://www.sec.gov/Archives/edgar/data/319201/"
        "000031920126000014/d12345dex991.htm"
    )
    client = _url_client({INDEX_URL: index_html, exhibit_url: ex991_html})

    a = article_factory(url=INDEX_URL, summary="", kind="edgar")
    body = _read_edgar(a, client)

    # The exhibit prose (earnings press release) is returned...
    assert "KLA Corporation Reports Fiscal 2026 Third Quarter Results" in body
    assert "Revenue of $3.10 billion" in body
    # ...not the SEC index chrome (nav/footer/document-table boilerplate).
    assert "Document Format Files" not in body
    assert "No FEAR Act" not in body
    assert "Complete submission text file" not in body


def test_read_edgar_uses_contact_user_agent(article_factory):
    index_html = (FIXTURES / "edgar_index_klac_8k.html").read_text(encoding="utf-8")
    ex991_html = (FIXTURES / "edgar_ex991_klac.html").read_text(encoding="utf-8")
    exhibit_url = (
        "https://www.sec.gov/Archives/edgar/data/319201/"
        "000031920126000014/d12345dex991.htm"
    )
    client = _url_client({INDEX_URL: index_html, exhibit_url: ex991_html})

    a = article_factory(url=INDEX_URL, kind="edgar")
    _read_edgar(a, client)

    # Every SEC fetch (index + exhibit) must carry the contact UA, else 403.
    for call in client.get.call_args_list:
        assert call.kwargs["headers"]["User-Agent"] == EDGAR_CONTACT_UA


def test_read_edgar_reads_main_doc_when_no_exhibit(article_factory):
    # A filing with no EX-99 exhibit: _read_edgar reads the main filing .htm,
    # whose prose it returns (not the index page).
    index_html = (FIXTURES / "edgar_index_no_exhibit.html").read_text(encoding="utf-8")
    main_html = (FIXTURES / "edgar_main_6k_asml.html").read_text(encoding="utf-8")
    main_url = (
        "https://www.sec.gov/Archives/edgar/data/319201/"
        "000031920126000014/asml6k.htm"
    )
    client = _url_client({INDEX_URL: index_html, main_url: main_html})

    a = article_factory(url=INDEX_URL, kind="edgar")
    body = _read_edgar(a, client)

    assert "Report of Foreign Private Issuer on Form 6-K" in body
    assert "EUV and High-NA lithography" in body


def test_read_edgar_falls_back_to_index_when_exhibit_unfetchable(article_factory):
    # The index names an EX-99 exhibit, but its URL 404s (not in the page map),
    # so _read_edgar must degrade to extracting the index page itself.
    index_html = (FIXTURES / "edgar_index_klac_8k.html").read_text(encoding="utf-8")
    client = _url_client({INDEX_URL: index_html})  # exhibit URL absent -> 404

    a = article_factory(url=INDEX_URL, kind="edgar")
    body = _read_edgar(a, client)

    # Something is returned (the index's own readable text), not "".
    assert body
    # The exhibit was attempted (index + exhibit = 2 gets) before falling back.
    assert client.get.call_count == 2


def test_read_edgar_index_unreachable_returns_empty(article_factory):
    # The index page itself 403s — nothing to read, degrade to "".
    client = _url_client({})  # no pages at all

    a = article_factory(url=INDEX_URL, kind="edgar")
    assert _read_edgar(a, client) == ""


def test_unwrap_ix_viewer_extracts_raw_doc_path():
    # The inline-XBRL viewer wrapper resolves to the raw document path...
    assert _unwrap_ix_viewer(
        "/ix?doc=/Archives/edgar/data/319201/000031920126000014/klac-20260429.htm"
    ) == "/Archives/edgar/data/319201/000031920126000014/klac-20260429.htm"
    # ...a host-qualified viewer URL works too...
    assert _unwrap_ix_viewer(
        "https://www.sec.gov/ix?doc=/Archives/edgar/data/1/2/doc.htm"
    ) == "/Archives/edgar/data/1/2/doc.htm"
    # ...and a plain (non-viewer) href is returned unchanged.
    assert _unwrap_ix_viewer(
        "/Archives/edgar/data/1/2/exhibit991.htm"
    ) == "/Archives/edgar/data/1/2/exhibit991.htm"


def test_find_edgar_primary_doc_unwraps_ix_viewer_fallback():
    # Modern index: the only document is linked through the iXBRL viewer and has
    # an absolute href. The chosen URL must be the RAW doc, not the /ix? wrapper.
    index_html = (FIXTURES / "edgar_index_ix_viewer.html").read_text(encoding="utf-8")

    doc_url = _find_edgar_primary_doc(index_html, INDEX_URL)

    assert doc_url == (
        "https://www.sec.gov/Archives/edgar/data/319201/"
        "000031920126000014/klac-20260429.htm"
    )
    # The viewer wrapper would have starved extraction of prose; ensure it's gone.
    assert "/ix?doc=" not in doc_url
    # Nav chrome links (/index.htm, companysearch.html) sit outside <tr> rows and
    # must never be chosen as the primary document.
    assert "companysearch" not in doc_url


def test_read_edgar_malformed_doc_href_does_not_crash(article_factory, mocker):
    # If the resolved exhibit URL is one httpx rejects off the HTTPError
    # hierarchy (InvalidURL / IDNA UnicodeError), _read_edgar must degrade to the
    # index text rather than letting the exception escape into enrich().
    index_html = (FIXTURES / "edgar_index_klac_8k.html").read_text(encoding="utf-8")

    def _get(url, *args, **kwargs):
        resp = MagicMock()
        if url == INDEX_URL:
            resp.text = index_html
            resp.headers = {"content-type": "text/html"}
            resp.raise_for_status.return_value = None
            return resp
        # The exhibit fetch raises an off-hierarchy URL error.
        raise httpx.InvalidURL("malformed exhibit URL")

    client = MagicMock()
    client.get.side_effect = _get

    a = article_factory(url=INDEX_URL, kind="edgar")
    body = _read_edgar(a, client)  # must not raise

    # Falls back to the index page's own text instead of crashing.
    assert body


def test_enrich_invalid_url_degrades_without_crashing(article_factory):
    # A non-edgar article whose URL httpx rejects with InvalidURL (not an
    # HTTPError) must degrade to body="" — enrich() never raises.
    a = article_factory(url="http://[::1", summary="teaser", kind="rss")
    client = MagicMock()
    client.get.side_effect = httpx.InvalidURL("malformed IPv6 host")

    fetched = enrich([a], client=client)

    assert fetched == 0
    assert a.body == ""
    assert a.summary == "teaser"


def test_enrich_routes_edgar_kind_through_read_edgar(article_factory):
    # End-to-end: an edgar-kind article in enrich() reads its exhibit prose.
    index_html = (FIXTURES / "edgar_index_klac_8k.html").read_text(encoding="utf-8")
    ex991_html = (FIXTURES / "edgar_ex991_klac.html").read_text(encoding="utf-8")
    exhibit_url = (
        "https://www.sec.gov/Archives/edgar/data/319201/"
        "000031920126000014/d12345dex991.htm"
    )
    client = _url_client({INDEX_URL: index_html, exhibit_url: ex991_html})

    a = article_factory(url=INDEX_URL, summary="", kind="edgar")
    fetched = enrich([a], client=client)

    assert fetched == 1
    assert "KLA Corporation Reports Fiscal 2026 Third Quarter Results" in a.body
