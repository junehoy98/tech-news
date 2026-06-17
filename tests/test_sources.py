from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from tech_news.sources import (
    INGESTORS,
    USER_AGENT,
    Source,
    fetch_all,
    fetch_source,
    filter_recent,
)

UTC = timezone.utc

NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_client(content: bytes) -> MagicMock:
    """An httpx.Client stand-in whose get() returns `content` and never errors."""
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    return client


def test_filter_recent_drops_old_keeps_fresh(article_factory):
    fresh = article_factory(url="https://example.com/fresh", published=NOW - timedelta(days=2))
    stale = article_factory(url="https://example.com/stale", published=NOW - timedelta(days=30))
    kept = filter_recent([fresh, stale], max_age_days=7, now=NOW)
    assert kept == [fresh]


def test_filter_recent_boundary_is_inclusive(article_factory):
    edge = article_factory(published=NOW - timedelta(days=7))
    assert filter_recent([edge], max_age_days=7, now=NOW) == [edge]


def test_filter_recent_disabled_keeps_everything(article_factory):
    old = article_factory(published=NOW - timedelta(days=365))
    assert filter_recent([old], max_age_days=0, now=NOW) == [old]


def test_filter_recent_empty_input(article_factory):
    assert filter_recent([], max_age_days=7, now=NOW) == []


def test_fetch_source_rss_parses_sample_feed():
    src = Source(name="Sample", url="https://example.com/feed", category="tech", priority=1)
    client = _fake_client((FIXTURES / "sample_feed.xml").read_bytes())

    articles = fetch_source(src, client)

    assert [a.url for a in articles] == [
        "https://example.com/asml-high-na",
        "https://example.com/kla-inspection",
    ]
    assert articles[0].title == "ASML ships first High-NA EUV tool"
    # HTML in the description is stripped down to text (tags gone, markup-free).
    assert "<" not in articles[0].summary
    assert "The tool reached the fab" in articles[0].summary
    assert "this week" in articles[0].summary
    # The second item's plain-text description passes through unchanged.
    assert articles[1].summary == "A plain-text summary with no markup."
    # Metadata is carried over from the Source.
    assert articles[0].source_name == "Sample"
    assert articles[0].category == "tech"
    assert articles[0].priority == 1
    # Provenance: a plain RSS item is tagged "rss" (the default and explicit).
    assert articles[0].kind == "rss"


def test_fetch_source_unknown_kind_returns_empty():
    src = Source(
        name="Mystery", url="https://example.com/x", category="tech", priority=1, kind="nope"
    )
    # No network call should be attempted for an unknown kind.
    client = MagicMock()
    assert fetch_source(src, client) == []
    client.get.assert_not_called()


def test_fetch_source_uses_per_source_user_agent():
    src = Source(
        name="EDGAR", url="https://example.com/feed", category="policy", priority=1,
        user_agent="contact me@example.com",
    )
    client = _fake_client((FIXTURES / "sample_feed.xml").read_bytes())

    fetch_source(src, client)

    _, kwargs = client.get.call_args
    assert kwargs["headers"]["User-Agent"] == "contact me@example.com"


def test_fetch_source_defaults_to_package_user_agent():
    src = Source(name="Sample", url="https://example.com/feed", category="tech", priority=1)
    client = _fake_client((FIXTURES / "sample_feed.xml").read_bytes())

    fetch_source(src, client)

    _, kwargs = client.get.call_args
    assert kwargs["headers"]["User-Agent"] == USER_AGENT


def test_rss_ingestor_is_registered():
    assert "rss" in INGESTORS


def test_fetch_all_isolates_a_crashing_source(article_factory, caplog):
    """An ingestor that raises a non-HTTPError must skip that source, not the run.

    The non-RSS adapters have many uncaught failure surfaces (a typo'd config CSS
    selector -> SelectorSyntaxError, a malformed options shape -> AttributeError).
    fetch_all must turn any such surprise into log+skip so the sources after it
    still get fetched.
    """
    good_article = article_factory(url="https://example.com/good")

    def _boom(source, client):
        raise ValueError("typo'd CSS selector")  # stands in for SelectorSyntaxError

    def _ok(source, client):
        return [good_article]

    boom_src = Source(name="Boom", url="https://x", category="company", priority=1, kind="boom")
    ok_src = Source(name="OK", url="https://y", category="tech", priority=1, kind="okkind")

    # Patch the registry with our fakes; fetch_all opens a real httpx.Client which
    # the fakes never touch, so this stays fully offline.
    with patch.dict(INGESTORS, {"boom": _boom, "okkind": _ok}):
        import logging

        with caplog.at_level(logging.WARNING):
            articles = fetch_all([boom_src, ok_src])

    # The crashing source contributed nothing; the healthy source after it survived.
    assert articles == [good_article]
    assert any("Boom" in r.message and "unexpected fetch error" in r.message for r in caplog.records)
