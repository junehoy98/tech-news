from datetime import datetime, timedelta, timezone

from tech_news.store import Store

UTC = timezone.utc


def test_first_seen_returns_all_new(tmp_path, article_factory):
    db = Store(tmp_path / "test.sqlite")
    articles = [
        article_factory(url=f"https://example.com/{i}", title=f"Item {i}")
        for i in range(3)
    ]
    assert db.filter_new(articles) == articles


def test_marked_articles_filtered_on_next_call(tmp_path, article_factory):
    db = Store(tmp_path / "test.sqlite")
    a, b, c = (
        article_factory(url="https://example.com/a"),
        article_factory(url="https://example.com/b"),
        article_factory(url="https://example.com/c"),
    )

    db.mark_seen([a, b])
    new = db.filter_new([a, b, c])
    assert new == [c]


def test_prune_drops_old_entries(tmp_path, article_factory):
    db = Store(tmp_path / "test.sqlite")
    a = article_factory(url="https://example.com/old")
    db.mark_seen([a])

    # Backdate the row directly
    import sqlite3

    with sqlite3.connect(db.db_path) as conn:
        old_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        conn.execute("UPDATE seen_articles SET seen_at = ?", (old_date,))
        conn.commit()

    removed = db.prune(retention_days=14)
    assert removed == 1
    # After prune, the article looks new again
    assert db.filter_new([a]) == [a]


def test_empty_inputs_handled_safely(tmp_path):
    db = Store(tmp_path / "test.sqlite")
    assert db.filter_new([]) == []
    db.mark_seen([])


def test_clear_wipes_all_entries(tmp_path, article_factory):
    db = Store(tmp_path / "test.sqlite")
    articles = [article_factory(url=f"https://example.com/{i}") for i in range(5)]
    db.mark_seen(articles)
    assert db.filter_new(articles) == []  # all seen

    removed = db.clear()
    assert removed == 5
    assert db.filter_new(articles) == articles  # all new again


def test_sent_marker_roundtrip(tmp_path):
    db = Store(tmp_path / "test.sqlite")
    assert db.already_sent("2026-05-28") is False
    db.mark_sent("2026-05-28")
    assert db.already_sent("2026-05-28") is True
    # A different day is unaffected, and re-marking is idempotent.
    assert db.already_sent("2026-05-29") is False
    db.mark_sent("2026-05-28")
    assert db.already_sent("2026-05-28") is True
