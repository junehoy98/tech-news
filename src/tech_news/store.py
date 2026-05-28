"""SQLite-backed dedupe store with a rolling 14-day window."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources import Article

UTC = timezone.utc

DEFAULT_RETENTION_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_articles (
    fingerprint TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    source_name TEXT NOT NULL,
    seen_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_articles(seen_at);

CREATE TABLE IF NOT EXISTS digest_sends (
    send_date TEXT PRIMARY KEY,
    sent_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def filter_new(self, articles: list[Article]) -> list[Article]:
        """Return only articles we haven't seen before."""
        if not articles:
            return []
        with self._connect() as conn:
            seen = {
                row[0]
                for row in conn.execute(
                    f"SELECT fingerprint FROM seen_articles WHERE fingerprint IN ({','.join('?' * len(articles))})",
                    [a.fingerprint for a in articles],
                )
            }
        return [a for a in articles if a.fingerprint not in seen]

    def mark_seen(self, articles: list[Article]) -> None:
        if not articles:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO seen_articles (fingerprint, url, title, source_name) "
                "VALUES (?, ?, ?, ?)",
                [(a.fingerprint, a.url, a.title, a.source_name) for a in articles],
            )

    def prune(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM seen_articles WHERE seen_at < ?",
                (cutoff.isoformat(),),
            )
            return cur.rowcount

    def clear(self) -> int:
        """Drop every seen entry. Used by --reset-seen during testing."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM seen_articles")
            return cur.rowcount

    def already_sent(self, day: str) -> bool:
        """True if a digest was already sent on `day` (an ISO date string).

        Lets a backup run detect that the day's digest already went out and
        bail before fetching or spending on the LLMs.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM digest_sends WHERE send_date = ?", (day,)
            ).fetchone()
            return row is not None

    def mark_sent(self, day: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO digest_sends (send_date) VALUES (?)", (day,)
            )
