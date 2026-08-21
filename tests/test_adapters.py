from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from tech_news.adapters import (
    EDGAR_FEED_URL,
    FEDERAL_REGISTER_URL,
    fetch_edgar,
    fetch_federal_register,
    fetch_scrape,
)
from tech_news.sources import INGESTORS, Source, fetch_source

UTC = timezone.utc

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_client(content: bytes) -> MagicMock:
    """An httpx.Client stand-in whose get() returns `content` and never errors."""
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    return client


def _json_client(text: str) -> MagicMock:
    """An httpx.Client stand-in whose get().json() parses `text` and never errors."""
    import json

    resp = MagicMock()
    resp.json.return_value = json.loads(text)
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    return client


def _edgar_source(filings, **opts) -> Source:
    options = {"filings": filings, **opts}
    return Source(
        name="SEC EDGAR (semi equipment)",
        url="",  # unused; EDGAR builds per-filer URLs from options
        category="company",
        priority=1,
        kind="edgar",
        user_agent="tech-news-digest (junehoy98@gmail.com)",
        options=options,
    )


def test_edgar_is_registered():
    assert INGESTORS.get("edgar") is fetch_edgar


def test_edgar_parses_us_8k_fixture():
    src = _edgar_source([{"ticker": "KLAC", "form": "8-K"}])
    client = _fake_client((FIXTURES / "edgar_8k_klac.xml").read_bytes())

    articles = fetch_source(src, client)

    # The fixture holds two 8-Ks: a routine one (Items 5.03/9.01 — bylaws) that
    # the newsworthiness filter drops, and a results one (Item 2.02) that stays.
    assert len(articles) == 1
    first = articles[0]
    # url is the filing-index link.
    assert first.url == (
        "https://www.sec.gov/Archives/edgar/data/319201/"
        "000031920126000014/0000319201-26-000014-index.htm"
    )
    # title is a readable filer + form + description string.
    assert first.title == "KLA CORP — 8-K: Current report"
    # summary is HTML-stripped (no tags) and carries the item descriptions.
    assert "<" not in first.summary
    assert "AccNo:" in first.summary
    assert "Item 2.02" in first.summary
    # Metadata is carried from the Source.
    assert first.source_name == "SEC EDGAR (semi equipment)"
    assert first.category == "company"
    assert first.priority == 1
    # Provenance: a filing is a PRIMARY source, tagged "edgar".
    assert first.kind == "edgar"


def test_edgar_published_is_tz_aware_utc():
    src = _edgar_source([{"ticker": "KLAC", "form": "8-K"}])
    client = _fake_client((FIXTURES / "edgar_8k_klac.xml").read_bytes())

    first = fetch_source(src, client)[0]

    # The surviving (Item 2.02) entry's <updated> was 2026-04-29T16:06:15-04:00;
    # normalized to UTC that's 20:06:15.
    assert first.published.tzinfo == UTC
    assert first.published.isoformat() == "2026-04-29T20:06:15+00:00"


def test_edgar_parses_foreign_6k_fixture():
    # Foreign filers (e.g. ASML) file 6-K with a sparser summary (no item list).
    src = _edgar_source([{"ticker": "ASML", "form": "6-K"}])
    client = _fake_client((FIXTURES / "edgar_6k_asml.xml").read_bytes())

    articles = fetch_source(src, client)

    assert len(articles) == 2
    assert articles[0].title == (
        "ASML HOLDING NV — 6-K: Report of foreign issuer [Rules 13a-16 and 15d-16]"
    )
    assert articles[0].summary == "Filed:  2026-04-23  AccNo:  0001628280-26-026703  Size:  35 KB"


def test_edgar_builds_per_filer_url_with_contact_ua():
    src = _edgar_source([{"ticker": "KLAC", "form": "8-K"}], count=5)
    client = _fake_client((FIXTURES / "edgar_8k_klac.xml").read_bytes())

    fetch_source(src, client)

    args, kwargs = client.get.call_args
    assert args[0] == EDGAR_FEED_URL.format(ticker="KLAC", form="8-K", count=5)
    # SEC returns 403 without a contact User-Agent, so it must be sent.
    assert kwargs["headers"]["User-Agent"] == "tech-news-digest (junehoy98@gmail.com)"


def test_edgar_fetches_each_filer_separately():
    src = _edgar_source(
        [{"ticker": "KLAC", "form": "8-K"}, {"ticker": "ASML", "form": "6-K"}]
    )
    client = _fake_client((FIXTURES / "edgar_8k_klac.xml").read_bytes())

    fetch_source(src, client)

    # One feed request per filer.
    assert client.get.call_count == 2
    requested = [call.args[0] for call in client.get.call_args_list]
    assert "CIK=KLAC&type=8-K" in requested[0]
    assert "CIK=ASML&type=6-K" in requested[1]


def test_edgar_per_filer_error_is_skipped_not_fatal():
    # First filer 403s, second returns a good feed: we keep the good one.
    good = MagicMock()
    good.content = (FIXTURES / "edgar_8k_klac.xml").read_bytes()
    good.raise_for_status.return_value = None

    bad = MagicMock()
    bad.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403 Forbidden", request=MagicMock(), response=MagicMock()
    )

    client = MagicMock()
    client.get.side_effect = [bad, good]

    src = _edgar_source(
        [{"ticker": "BADTICKER", "form": "8-K"}, {"ticker": "KLAC", "form": "8-K"}]
    )

    articles = fetch_source(src, client)

    # The 403 filer contributes nothing; the healthy filer's newsworthy
    # item survives (the routine 5.03 filing is filtered).
    assert len(articles) == 1
    assert all(a.source_name == src.name for a in articles)


def test_edgar_skips_malformed_filing_entry():
    # A filings entry missing ticker/form is logged and skipped, not fatal.
    src = _edgar_source([{"ticker": "KLAC"}, {"ticker": "KLAC", "form": "8-K"}])
    client = _fake_client((FIXTURES / "edgar_8k_klac.xml").read_bytes())

    articles = fetch_source(src, client)

    # Only the well-formed entry triggers a fetch.
    assert client.get.call_count == 1
    assert len(articles) == 1


def test_edgar_item_newsworthiness_filter():
    from tech_news.adapters import _edgar_items_newsworthy

    # Routine-only 8-K items are dropped...
    assert not _edgar_items_newsworthy(
        "8-K", "Item 5.02: Departure of Directors<br>Item 9.01: Exhibits"
    )
    # ...one newsworthy item rescues the entry...
    assert _edgar_items_newsworthy(
        "8-K", "Item 5.03: Bylaws<br>Item 8.01: Other Events"
    )
    # ...no parseable items keeps the entry, and non-8-K forms always pass.
    assert _edgar_items_newsworthy("8-K", "no items here")
    assert _edgar_items_newsworthy("6-K", "Item 5.02: whatever")


def test_edgar_no_filings_returns_empty():
    src = _edgar_source([])
    client = _fake_client(b"")

    assert fetch_source(src, client) == []
    client.get.assert_not_called()


def _federal_register_source(**opts) -> Source:
    return Source(
        name="Federal Register (BIS)",
        url="",  # unused; the adapter builds the query URL from options
        category="policy",
        priority=1,
        kind="federal_register",
        options=opts,
    )


def test_federal_register_is_registered():
    assert INGESTORS.get("federal_register") is fetch_federal_register


def test_federal_register_parses_fixture():
    src = _federal_register_source(
        agencies=["industry-and-security-bureau"], term="semiconductor"
    )
    client = _json_client((FIXTURES / "federal_register_bis.json").read_text(encoding="utf-8"))

    articles = fetch_source(src, client)

    assert len(articles) == 3
    first = articles[0]
    assert first.title == "Revision to License Review Policy for Advanced Computing Commodities"
    # url is the document's html_url.
    assert first.url == (
        "https://www.federalregister.gov/documents/2026/01/15/2026-00789/"
        "revision-to-license-review-policy-for-advanced-computing-commodities"
    )
    # summary comes from the abstract.
    assert first.summary.startswith("The Bureau of Industry and Security (BIS) is revising")
    # Metadata is carried from the Source.
    assert first.source_name == "Federal Register (BIS)"
    assert first.category == "policy"
    assert first.priority == 1
    # Provenance: a Federal Register notice is tagged "federal_register".
    assert first.kind == "federal_register"


def test_federal_register_published_is_tz_aware_utc():
    src = _federal_register_source(agencies=["industry-and-security-bureau"])
    client = _json_client((FIXTURES / "federal_register_bis.json").read_text(encoding="utf-8"))

    first = fetch_source(src, client)[0]

    # publication_date "2026-01-15" is anchored to midnight UTC.
    assert first.published.tzinfo == UTC
    assert first.published.isoformat() == "2026-01-15T00:00:00+00:00"


def test_federal_register_builds_conditions_params():
    src = _federal_register_source(
        agencies=["industry-and-security-bureau", "foreign-assets-control-office"],
        term="semiconductor",
        per_page=5,
    )
    client = _json_client((FIXTURES / "federal_register_bis.json").read_text(encoding="utf-8"))

    fetch_source(src, client)

    args, kwargs = client.get.call_args
    assert args[0] == FEDERAL_REGISTER_URL
    params = kwargs["params"]
    # Each agency repeats conditions[agencies][] so httpx encodes them as a list.
    assert params.count(("conditions[agencies][]", "industry-and-security-bureau")) == 1
    assert params.count(("conditions[agencies][]", "foreign-assets-control-office")) == 1
    assert ("conditions[term]", "semiconductor") in params
    assert ("per_page", 5) in params
    assert ("order", "newest") in params


def test_federal_register_http_error_is_skipped_not_fatal():
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500 Server Error", request=MagicMock(), response=MagicMock()
    )
    client = MagicMock()
    client.get.return_value = resp

    src = _federal_register_source(agencies=["industry-and-security-bureau"])

    assert fetch_source(src, client) == []


def test_federal_register_malformed_json_is_skipped_not_fatal():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.side_effect = ValueError("Expecting value")
    client = MagicMock()
    client.get.return_value = resp

    src = _federal_register_source(agencies=["industry-and-security-bureau"])

    assert fetch_source(src, client) == []


def test_federal_register_skips_results_missing_url_or_title():
    payload = (
        '{"results": ['
        '{"title": "No URL", "abstract": "x", "publication_date": "2026-01-15"},'
        '{"html_url": "https://example.com/a", "title": "", "publication_date": "2026-01-15"},'
        '{"html_url": "https://example.com/b", "title": "Good", "abstract": null,'
        ' "publication_date": "2026-01-15"}'
        "]}"
    )
    src = _federal_register_source(agencies=["industry-and-security-bureau"])
    client = _json_client(payload)

    articles = fetch_source(src, client)

    # Only the third result is well-formed; a null abstract yields an empty summary.
    assert len(articles) == 1
    assert articles[0].title == "Good"
    assert articles[0].summary == ""


# --------------------------------------------------------------------------- #
# HTML-scrape adapter (fetch_scrape)                                           #
#                                                                             #
# These run fully offline against saved fixture snippets — NEVER the network.  #
# The selectors below match the live sites as of June 2026 and are BRITTLE: if #
# a newsroom re-skins, the fix is in config/sources.toml, and these fixtures   #
# (tests/fixtures/scrape_*.html) plus the expectations here would be refreshed #
# from a new capture.                                                          #
# --------------------------------------------------------------------------- #


def _text_client(text: str) -> MagicMock:
    """An httpx.Client stand-in whose get().text returns `text` and never errors."""
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    return client


def _asml_source(**opts) -> Source:
    # Mirrors config/sources.toml's "ASML (press releases)" source: the item IS
    # the <a>, so link is "" (use the item itself); date is text-only.
    options = {
        "page_url": "https://www.asml.com/en/news/press-releases",
        "base_url": "https://www.asml.com",
        "item": "a.related-content-card--story",
        "title": ".related-content-card-title",
        "link": "",
        "date": ".related-content-card-subtitle",
        **opts,
    }
    return Source(
        name="ASML (press releases)",
        url="https://www.asml.com/en/news/press-releases",
        category="company",
        priority=1,
        kind="scrape",
        user_agent="Mozilla/5.0 (browser-like)",
        options=options,
    )


def _kla_source(**opts) -> Source:
    # Mirrors "KLA (newsroom)": item is a div, title/link is the inner <a>, and
    # the date is a <time datetime="..."> the adapter prefers over the label.
    options = {
        "page_url": "https://www.kla.com/newsroom",
        "base_url": "https://www.kla.com",
        "item": "div.news-detail",
        "title": "a",
        "link": "a",
        "date": "time",
        **opts,
    }
    return Source(
        name="KLA (newsroom)",
        url="https://www.kla.com/newsroom",
        category="company",
        priority=1,
        kind="scrape",
        user_agent="Mozilla/5.0 (browser-like)",
        options=options,
    )


def test_scrape_is_registered():
    assert INGESTORS.get("scrape") is fetch_scrape


def test_scrape_parses_asml_fixture():
    src = _asml_source()
    client = _text_client((FIXTURES / "scrape_asml.html").read_text(encoding="utf-8"))

    articles = fetch_source(src, client)

    # Four cards in the fixture; the last has no href and is skipped.
    assert len(articles) == 3
    first = articles[0]
    assert first.title == (
        "Tata Electronics and ASML Announce Strategic Partnership to Advance "
        "the Semiconductor Manufacturing Ecosystem in India"
    )
    # The item <a> itself carries the absolute href.
    assert first.url == (
        "https://www.asml.com/en/news/press-releases/2026/"
        "tata-electronics-and-asml-announce-strategic-partnership"
    )
    # Listing pages have no blurb; summary is empty by design.
    assert first.summary == ""
    # Metadata is carried from the Source.
    assert first.source_name == "ASML (press releases)"
    assert first.category == "company"
    assert first.priority == 1
    # Provenance: a scraped newsroom release is a PRIMARY source, tagged "scrape".
    assert first.kind == "scrape"


def test_scrape_resolves_relative_link_against_base_url():
    src = _asml_source()
    client = _text_client((FIXTURES / "scrape_asml.html").read_text(encoding="utf-8"))

    articles = fetch_source(src, client)

    # The third card uses a relative href; it's resolved against base_url.
    third = articles[2]
    assert third.url == (
        "https://www.asml.com/en/news/press-releases/2026/asml-discloses-2026-agm-results"
    )


def test_scrape_parses_text_date_to_tz_aware_utc():
    src = _asml_source()
    client = _text_client((FIXTURES / "scrape_asml.html").read_text(encoding="utf-8"))

    first = fetch_source(src, client)[0]

    # "May 16, 2026" has no time/zone -> anchored to midnight UTC.
    assert first.published.tzinfo == UTC
    assert first.published.isoformat() == "2026-05-16T00:00:00+00:00"


def test_scrape_skips_item_without_link():
    src = _asml_source()
    client = _text_client((FIXTURES / "scrape_asml.html").read_text(encoding="utf-8"))

    articles = fetch_source(src, client)

    # The malformed (href-less) card never becomes an Article.
    assert all("Malformed card" not in a.title for a in articles)


def test_scrape_sends_browser_user_agent():
    src = _asml_source()
    client = _text_client((FIXTURES / "scrape_asml.html").read_text(encoding="utf-8"))

    fetch_source(src, client)

    args, kwargs = client.get.call_args
    assert args[0] == "https://www.asml.com/en/news/press-releases"
    # These CDNs 403 the default UA, so the per-source browser UA must be sent.
    assert kwargs["headers"]["User-Agent"] == "Mozilla/5.0 (browser-like)"


def test_scrape_prefers_time_datetime_attribute():
    # KLA stamps a machine-readable <time datetime="..."> with an explicit zone;
    # the adapter uses it (not the "May 13, 2026" label) and keeps the time.
    src = _kla_source()
    client = _text_client((FIXTURES / "scrape_kla.html").read_text(encoding="utf-8"))

    articles = fetch_source(src, client)

    assert len(articles) == 2
    first = articles[0]
    assert first.title == "KLA Announces Upcoming Investor Webcasts"
    assert first.url == (
        "https://ir.kla.com/news-events/press-releases/detail/516/"
        "kla-announces-upcoming-investor-webcasts"
    )
    assert first.published.tzinfo == UTC
    assert first.published.isoformat() == "2026-05-13T20:00:00+00:00"


def test_scrape_missing_item_selector_returns_empty(caplog):
    src = _asml_source(item="")
    client = _text_client((FIXTURES / "scrape_asml.html").read_text(encoding="utf-8"))

    assert fetch_source(src, client) == []


def test_scrape_item_selector_matches_nothing_returns_empty(caplog):
    # Layout drift: the item selector matches no element -> warn + [], never throw.
    src = _asml_source(item=".no-such-class")
    client = _text_client((FIXTURES / "scrape_asml.html").read_text(encoding="utf-8"))

    import logging

    with caplog.at_level(logging.WARNING):
        articles = fetch_source(src, client)

    assert articles == []
    assert any("matched nothing" in r.message for r in caplog.records)


def test_scrape_empty_date_selector_defaults_to_now():
    # No date selector configured -> default to now() (tz-aware), item still kept.
    before = datetime.now(UTC)
    src = _asml_source(date="")
    client = _text_client((FIXTURES / "scrape_asml.html").read_text(encoding="utf-8"))

    first = fetch_source(src, client)[0]
    after = datetime.now(UTC)

    assert first.published.tzinfo == UTC
    assert before <= first.published <= after


def test_scrape_http_error_is_skipped_not_fatal():
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403 Forbidden", request=MagicMock(), response=MagicMock()
    )
    client = MagicMock()
    client.get.return_value = resp

    # SEMI.org's Cloudflare challenge looks like this in production.
    src = _asml_source()

    assert fetch_source(src, client) == []


def test_scrape_no_page_url_returns_empty():
    src = Source(
        name="broken scrape",
        url="",
        category="company",
        priority=1,
        kind="scrape",
        options={"item": "div"},  # no page_url, and url is empty
    )
    client = _text_client("<html></html>")

    assert fetch_source(src, client) == []
    client.get.assert_not_called()


def test_hn_algolia_builds_articles_and_dedupes_across_queries():
    from tech_news.adapters import fetch_hn_algolia

    hits_page = {
        "hits": [
            {
                "title": "ASML ships first High-NA tool",
                "url": "https://example.com/asml",
                "objectID": "1",
                "created_at": "2026-08-20T20:39:07Z",
                "story_text": None,
            },
            {
                "title": "Ask HN: EUV explained?",
                "url": None,
                "objectID": "42",
                "created_at": "2026-08-19T10:00:00Z",
                "story_text": "<p>some text</p>",
            },
        ]
    }
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = hits_page
    client = MagicMock()
    client.get.return_value = resp

    src = Source(
        name="HN",
        url="https://hn.algolia.com/api/v1/search_by_date",
        category="tech",
        priority=1,
        kind="hn_algolia",
        options={"queries": ["ASML", "EUV"], "min_points": 5},
    )
    articles = fetch_hn_algolia(src, client)

    # Two queries, same hits page returned for both -> deduped by URL.
    assert client.get.call_count == 2
    assert len(articles) == 2
    assert articles[0].url == "https://example.com/asml"
    assert articles[0].published.isoformat() == "2026-08-20T20:39:07+00:00"
    # Link-less story falls back to the HN discussion page; HTML is stripped.
    assert articles[1].url == "https://news.ycombinator.com/item?id=42"
    assert "<p>" not in articles[1].summary
    # The noise-control params must be present on the API call.
    params = client.get.call_args.kwargs.get("params") or client.get.call_args.args[1]
    assert params["restrictSearchableAttributes"] == "title"
    assert params["numericFilters"] == "points>=5"


def test_hn_algolia_failed_query_is_skipped():
    from tech_news.adapters import fetch_hn_algolia

    bad = MagicMock()
    bad.raise_for_status.side_effect = httpx.HTTPStatusError(
        "502", request=MagicMock(), response=MagicMock()
    )
    good = MagicMock()
    good.raise_for_status.return_value = None
    good.json.return_value = {
        "hits": [
            {
                "title": "KLA acquires a metrology startup",
                "url": "https://example.com/kla",
                "objectID": "7",
                "created_at": "2026-08-18T00:00:00Z",
            }
        ]
    }
    client = MagicMock()
    client.get.side_effect = [bad, good]
    src = Source(
        name="HN",
        url="x",
        category="tech",
        priority=1,
        kind="hn_algolia",
        options={"queries": ["ASML", "KLA"]},
    )
    articles = fetch_hn_algolia(src, client)
    assert [a.url for a in articles] == ["https://example.com/kla"]
