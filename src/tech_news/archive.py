"""Per-day digest archive: one JSON line per sent digest.

This is the durable record of what actually went out — the substrate for
future storyline threading (linking today's brief to last week's on the same
topic) and a growing corpus to mine. It is intentionally append-only and
schema-light: a JSON Lines file where each line is one digest.

Writing happens only after a real send (see main.py); dry runs and "nothing
new" days leave no trace. Reading tolerates a missing or empty file so the
first run, or a wiped archive, is not an error.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .synthesize import Digest

UTC = timezone.utc

log = logging.getLogger(__name__)


def _digest_record(digest: Digest, ranked: list) -> dict:
    """Shape one digest (plus the ranked pool that fed it) into a JSON dict.

    `ranked` is the list[RankedArticle] handed to the synthesizer; we keep the
    distinct topic_tags off it so a later threading pass can match stories
    across days without re-deriving them.
    """
    def _brief_dict(b) -> dict:
        return {
            "headline": b.headline,
            "paragraph": b.paragraph,
            "category": b.category,
            "citations": [{"source": c.source, "url": c.url} for c in b.citations],
        }

    briefs = [_brief_dict(b) for b in digest.briefs]
    # The longer lead brief is recorded under its own key so a later threading
    # pass can tell the day's headline story from the rest. Null on a "nothing
    # qualified" day (empty digest).
    lead_brief = _brief_dict(digest.lead_brief) if digest.lead_brief else None

    # Distinct topic_tags, in first-seen order, skipping the empty tag the
    # ranker assigns to omitted/unscored items.
    topic_tags: list[str] = []
    for r in ranked:
        tag = getattr(r, "topic_tag", "")
        if tag and tag not in topic_tags:
            topic_tags.append(tag)

    return {
        "date": digest.date.isoformat(),
        "email_subject": digest.email_subject,
        "intro": digest.intro,
        "lead_brief": lead_brief,
        "briefs": briefs,
        "topic_tags": topic_tags,
    }


def append_digest(digest: Digest, ranked: list, path: Path) -> None:
    """Append one digest as a single JSON line to the archive at `path`.

    Creates the parent directory and the file on first write. A failure here
    must not sink an otherwise-successful send, so any error is logged and
    swallowed — the digest already went out; the archive is a best-effort record.
    """
    try:
        record = _digest_record(digest, ranked)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        log.info("Archived digest for %s (%d briefs) to %s", record["date"], len(record["briefs"]), path)
    except Exception:  # noqa: BLE001 — never let archiving break a sent run
        log.exception("Failed to archive digest to %s", path)


def read_recent(path: Path, days: int) -> list[dict]:
    """Return archived digests from the last `days`, oldest first.

    Tolerant of a missing or empty file (returns []), and of an occasional
    malformed line (logged and skipped) so one bad write can't poison reads.
    A record with an unparseable `date` is kept rather than dropped, on the
    assumption a recent write is more likely than an ancient one.
    """
    if not path.exists():
        return []

    cutoff = datetime.now(UTC).date() - timedelta(days=days)
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Skipping malformed archive line in %s", path)
                continue
            if _within_window(record.get("date"), cutoff):
                records.append(record)

    records.sort(key=lambda r: r.get("date", ""))
    return records


def _within_window(date_str: object, cutoff: date) -> bool:
    """True if `date_str` (ISO date) is on or after `cutoff`.

    An unparseable or missing date returns True so the record is kept — better
    to over-include in the window than to silently drop a recent digest whose
    date field got mangled.
    """
    if not isinstance(date_str, str):
        return True
    try:
        parsed = date.fromisoformat(date_str)
    except ValueError:
        return True
    return parsed >= cutoff
