"""Offline tests for the per-day digest archive (append + read_recent)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from tech_news.archive import append_digest, read_recent
from tech_news.rank import RankedArticle
from tech_news.synthesize import Brief, Citation, Digest, LeadBrief

UTC = timezone.utc


def _digest(
    d: date = date(2026, 6, 16),
    subject: str = "ASML ships High-NA; BIS tightens export rules",
    intro: str = "A busy day in lithography and policy.",
) -> Digest:
    lead = LeadBrief(
        headline="ASML ships its first High-NA system",
        paragraph=(
            "**ASML** delivered its first High-NA EUV tool, lifting numerical "
            "aperture to **0.55** and shrinking the printable half-pitch toward 8 nm."
        ),
        citations=[
            Citation(source="SemiWiki", url="https://semiwiki.com/asml"),
            Citation(source="ASML", url="https://asml.com/highna"),
        ],
        category="company",
    )
    briefs = [
        Brief(
            headline="BIS widens semiconductor export controls",
            paragraph="The **Bureau of Industry and Security** added new line items.",
            citations=[Citation(source="Federal Register", url="https://fr.gov/bis")],
            category="policy",
        ),
    ]
    return Digest(
        date=d,
        email_subject=subject,
        intro=intro,
        lead_brief=lead,
        briefs=briefs,
        total_kept=2,
        total_fetched=10,
    )


def _ranked(article_factory, tags: list[str]) -> list[RankedArticle]:
    out = []
    for i, tag in enumerate(tags):
        out.append(
            RankedArticle(
                article=article_factory(url=f"https://example.com/{i}"),
                score=8,
                category="company",
                topic_tag=tag,
            )
        )
    return out


def test_append_then_read_roundtrip(tmp_path, article_factory):
    path = tmp_path / "digest_archive.jsonl"
    digest = _digest()
    ranked = _ranked(
        article_factory,
        ["ASML High-NA shipment", "BIS export rules", "ASML High-NA shipment", ""],
    )

    append_digest(digest, ranked, path)

    records = read_recent(path, days=30)
    assert len(records) == 1
    rec = records[0]
    assert rec["date"] == "2026-06-16"
    assert rec["email_subject"] == digest.email_subject
    assert rec["intro"] == digest.intro

    # The lead brief round-trips under its own key, with citations and category.
    lead = rec["lead_brief"]
    assert lead["headline"] == "ASML ships its first High-NA system"
    assert lead["category"] == "company"
    assert lead["citations"] == [
        {"source": "SemiWiki", "url": "https://semiwiki.com/asml"},
        {"source": "ASML", "url": "https://asml.com/highna"},
    ]

    # Regular briefs round-trip too (lead is NOT duplicated here).
    assert len(rec["briefs"]) == 1
    assert rec["briefs"][0]["headline"] == "BIS widens semiconductor export controls"
    assert rec["briefs"][0]["category"] == "policy"

    # Distinct topic_tags, deduped and order-preserved, empty tag dropped.
    assert rec["topic_tags"] == ["ASML High-NA shipment", "BIS export rules"]


def test_append_is_one_json_line_per_digest(tmp_path, article_factory):
    path = tmp_path / "digest_archive.jsonl"
    ranked = _ranked(article_factory, ["t1"])

    append_digest(_digest(d=date(2026, 6, 14)), ranked, path)
    append_digest(_digest(d=date(2026, 6, 15)), ranked, path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # Each line is independently valid JSON.
    for line in lines:
        json.loads(line)


def test_read_recent_filters_by_window(tmp_path, article_factory):
    path = tmp_path / "digest_archive.jsonl"
    ranked = _ranked(article_factory, ["t1"])

    today = datetime.now(UTC).date()
    recent_day = today - timedelta(days=2)
    old_day = today - timedelta(days=40)

    append_digest(_digest(d=old_day), ranked, path)
    append_digest(_digest(d=recent_day), ranked, path)

    records = read_recent(path, days=7)
    assert len(records) == 1
    assert records[0]["date"] == recent_day.isoformat()


def test_read_recent_orders_oldest_first(tmp_path, article_factory):
    path = tmp_path / "digest_archive.jsonl"
    ranked = _ranked(article_factory, ["t1"])

    today = datetime.now(UTC).date()
    d1 = today - timedelta(days=1)
    d3 = today - timedelta(days=3)

    # Write newest first; read_recent should sort oldest -> newest.
    append_digest(_digest(d=d1), ranked, path)
    append_digest(_digest(d=d3), ranked, path)

    records = read_recent(path, days=30)
    assert [r["date"] for r in records] == [d3.isoformat(), d1.isoformat()]


def test_read_recent_missing_file_returns_empty(tmp_path):
    path = tmp_path / "does_not_exist.jsonl"
    assert read_recent(path, days=7) == []


def test_read_recent_empty_file_returns_empty(tmp_path):
    path = tmp_path / "digest_archive.jsonl"
    path.write_text("", encoding="utf-8")
    assert read_recent(path, days=7) == []


def test_read_recent_skips_malformed_line(tmp_path, article_factory):
    path = tmp_path / "digest_archive.jsonl"
    append_digest(_digest(), _ranked(article_factory, ["t1"]), path)
    # Corrupt the file with a junk line and a blank line.
    with open(path, "a", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write("\n")

    records = read_recent(path, days=3650)
    assert len(records) == 1
    assert records[0]["email_subject"]


def test_append_tolerates_empty_ranked(tmp_path):
    path = tmp_path / "digest_archive.jsonl"
    append_digest(_digest(), [], path)
    records = read_recent(path, days=30)
    assert len(records) == 1
    assert records[0]["topic_tags"] == []


def test_append_records_null_lead_when_absent(tmp_path, article_factory):
    path = tmp_path / "digest_archive.jsonl"
    digest = Digest(
        date=date(2026, 6, 16),
        email_subject="no qualifying news",
        intro="Nothing crossed the threshold.",
        lead_brief=None,
        briefs=[],
        total_kept=0,
        total_fetched=5,
    )
    append_digest(digest, [], path)
    records = read_recent(path, days=30)
    assert len(records) == 1
    assert records[0]["lead_brief"] is None
    assert records[0]["briefs"] == []
