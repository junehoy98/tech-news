"""SQLite-backed dedupe store with a rolling 14-day window."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources import Article

UTC = timezone.utc

# 45 days (was 14): scraped press-release listings whose dates fail to parse
# fall back to "now" and would resurface as new right after their fingerprint
# was pruned. A longer memory closes that loop cheaply.
DEFAULT_RETENTION_DAYS = 45

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

-- Per-source item counts per run day, for detecting a source that silently
-- died (selector re-skin, feed 5xx streak) by its yield dropping to zero.
CREATE TABLE IF NOT EXISTS source_yields (
    source_name TEXT NOT NULL,
    run_date    TEXT NOT NULL,
    items       INTEGER NOT NULL,
    PRIMARY KEY (source_name, run_date)
);
"""

# A source warns when its latest CONSECUTIVE_ZERO_RUNS recorded run-days
# (including today) all yielded zero AND it yielded something within
# YIELD_HISTORY_DAYS — i.e. it used to work and has now gone quiet.
CONSECUTIVE_ZERO_RUNS = 3
YIELD_HISTORY_DAYS = 30


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

    def count_seen(self) -> int:
        """Number of fingerprints currently in the dedupe table."""
        with self._connect() as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM seen_articles").fetchone()
            return n

    def mark_seen_urls(self, urls: list[str], label: str = "(seeded from archive)") -> None:
        """Mark bare URLs as seen — used to reseed after a cache eviction.

        Fingerprints match Article.fingerprint (sha256(url)[:16]) so a real
        fetch of the same URL dedupes against the seeded row.
        """
        import hashlib

        if not urls:
            return
        rows = [
            (hashlib.sha256(u.encode("utf-8")).hexdigest()[:16], u, label, label)
            for u in urls
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO seen_articles (fingerprint, url, title, source_name) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )

    def record_yields(self, counts: dict[str, int], run_date: str) -> None:
        """Record how many items each source produced on `run_date` (ISO)."""
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO source_yields (source_name, run_date, items) "
                "VALUES (?, ?, ?)",
                [(name, run_date, n) for name, n in counts.items()],
            )

    def zero_yield_warnings(
        self,
        counts: dict[str, int],
        run_date: str,
        consecutive: int = CONSECUTIVE_ZERO_RUNS,
        history_days: int = YIELD_HISTORY_DAYS,
    ) -> list[str]:
        """Human-readable warnings for sources that used to yield but went quiet.

        Call AFTER record_yields for the same run_date. A source warns when its
        `consecutive` most recent recorded run-days (including run_date) are all
        zero and it produced at least one item within `history_days`.
        """
        cutoff = (
            datetime.fromisoformat(run_date) - timedelta(days=history_days)
        ).date().isoformat()
        warnings: list[str] = []
        with self._connect() as conn:
            for name in sorted(counts):
                if counts[name] > 0:
                    continue
                rows = conn.execute(
                    "SELECT run_date, items FROM source_yields "
                    "WHERE source_name = ? AND run_date >= ? AND run_date <= ? "
                    "ORDER BY run_date DESC",
                    (name, cutoff, run_date),
                ).fetchall()
                recent = rows[:consecutive]
                if len(recent) < consecutive or any(items > 0 for _, items in recent):
                    continue
                last_ok = next((d for d, items in rows if items > 0), None)
                if last_ok is None:
                    continue  # never yielded in the window — a config problem, not a regression
                warnings.append(
                    f"{name}: 0 items for {len(recent)}+ straight runs (last items {last_ok})"
                )
        return warnings

    def mark_sent(self, day: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO digest_sends (send_date) VALUES (?)", (day,)
            )
