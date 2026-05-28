from datetime import datetime, timedelta, timezone

from tech_news.sources import filter_recent

UTC = timezone.utc

NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


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
