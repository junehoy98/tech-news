"""Offline tests for the synthesis pass — the client is mocked, no network.

These pin the new lead-brief shape: synthesize() must surface the model's
`lead_brief` onto the Digest, keep the regular `briefs` separate, and pass
the lead-brief instructions through to the candidate prompt. They also pin
storyline threading: a seeded archive builds a "previously reported" block
into the prompt, while a cold start (missing/empty archive) leaves it out.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from tech_news.rank import RankedArticle
from tech_news.synthesize import (
    Brief,
    Citation,
    LeadBrief,
    SynthesisResponse,
    _format_candidates,
    synthesize,
)

CRITERIA = Path(__file__).resolve().parents[1] / "config" / "criteria.md"
UTC = timezone.utc


def _ranked(article_factory, n: int = 3) -> list[RankedArticle]:
    out = []
    for i in range(n):
        out.append(
            RankedArticle(
                article=article_factory(
                    url=f"https://example.com/{i}",
                    title=f"Article {i}",
                    summary="Some semiconductor equipment news.",
                ),
                score=8,
                category="tech",
                topic_tag=f"tag{i}",
            )
        )
    return out


class _FakeMessages:
    """Stand-in for client.messages whose .parse returns a canned response."""

    def __init__(self, parsed: SynthesisResponse | None):
        self._parsed = parsed
        self.last_kwargs: dict | None = None

    def parse(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            parsed_output=self._parsed,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=100, output_tokens=200),
        )


class _FakeClient:
    def __init__(self, parsed: SynthesisResponse | None):
        self.messages = _FakeMessages(parsed)


def _response() -> SynthesisResponse:
    return SynthesisResponse(
        email_subject="ASML ships High-NA; BIS tightens export rules",
        intro="Lithography and policy share the morning.",
        lead_brief=LeadBrief(
            headline="ASML ships its first High-NA EUV scanner",
            paragraph=(
                "**ASML** shipped its first High-NA EUV scanner, raising numerical "
                "aperture to **0.55** so the printable half-pitch shrinks toward 8 nm "
                "at the same 13.5 nm wavelength."
            ),
            citations=[Citation(source="SemiWiki", url="https://example.com/0")],
            category="tech",
        ),
        briefs=[
            Brief(
                headline="BIS widens export controls",
                paragraph="The **Bureau of Industry and Security** added line items.",
                citations=[Citation(source="Federal Register", url="https://example.com/1")],
                category="policy",
            ),
        ],
    )


def test_synthesize_surfaces_lead_brief_and_regular_briefs(article_factory, tmp_path):
    client = _FakeClient(_response())
    digest = synthesize(
        _ranked(article_factory),
        criteria_path=CRITERIA,
        total_fetched=3,
        client=client,
        archive_path=tmp_path / "no_archive.jsonl",
    )

    assert digest.lead_brief is not None
    assert digest.lead_brief.headline == "ASML ships its first High-NA EUV scanner"
    assert "numerical" in digest.lead_brief.paragraph

    # The lead is NOT duplicated into the regular briefs.
    assert len(digest.briefs) == 1
    assert digest.briefs[0].headline == "BIS widens export controls"

    assert digest.email_subject.startswith("ASML")
    assert digest.total_kept == 3
    assert digest.total_fetched == 3


def test_synthesize_empty_when_no_candidates(article_factory):
    # All below MIN_SCORE_FOR_SYNTHESIS -> no candidates -> empty digest, no call.
    low = [
        RankedArticle(article=article_factory(), score=2, category="tech", topic_tag="x")
    ]
    client = _FakeClient(_response())
    digest = synthesize(low, criteria_path=CRITERIA, total_fetched=1, client=client)

    assert digest.lead_brief is None
    assert digest.briefs == []
    assert digest.total_kept == 0
    # The model was never called.
    assert client.messages.last_kwargs is None


def test_synthesize_empty_on_unparsed_response(article_factory, tmp_path):
    client = _FakeClient(None)  # parsed_output is None
    digest = synthesize(
        _ranked(article_factory),
        criteria_path=CRITERIA,
        total_fetched=3,
        client=client,
        archive_path=tmp_path / "no_archive.jsonl",
    )
    assert digest.lead_brief is None
    assert digest.briefs == []


def test_format_candidates_mentions_lead_brief(article_factory):
    prompt = _format_candidates(_ranked(article_factory), target_briefs=7)
    assert "LEAD brief" in prompt
    assert "120-180" in prompt
    # The acceptable range for regular briefs is conveyed.
    assert "6-8" in prompt


def test_format_candidates_marks_primary_sources(article_factory):
    # One scrape (company press release), one edgar (SEC filing), one plain RSS.
    candidates = [
        RankedArticle(
            article=article_factory(
                url="https://asml.com/pr", title="ASML press release", kind="scrape"
            ),
            score=9, category="company", topic_tag="ASML High-NA",
        ),
        RankedArticle(
            article=article_factory(
                url="https://sec.gov/filing", title="ASML 8-K", kind="edgar"
            ),
            score=8, category="company", topic_tag="ASML High-NA",
        ),
        RankedArticle(
            article=article_factory(
                url="https://semiwiki.com/post", title="SemiWiki coverage", kind="rss"
            ),
            score=7, category="company", topic_tag="ASML High-NA",
        ),
    ]
    prompt = _format_candidates(candidates, target_briefs=7)

    # The marker renders on the primary candidates' source lines.
    assert "ASML press release" in prompt
    assert "[PRIMARY SOURCE]" in prompt
    # Both primary kinds are marked: the marker appears at least twice (scrape + edgar).
    assert prompt.count("[PRIMARY SOURCE]") >= 2

    # The plain-RSS candidate's source line is NOT marked.
    rss_source_line = next(
        ln for ln in prompt.splitlines()
        if ln.startswith("source:") and "TestSource" in ln and "[PRIMARY SOURCE]" not in ln
    )
    assert rss_source_line is not None

    # The citation instruction is present and tells the model to keep the primary link.
    assert "Primary sources:" in prompt
    assert "alongside the trade-press citations" in prompt
    assert "never invent a citation" in prompt


def _seed_archive(path: Path, *, day: date, tags: list[str], briefs: list[dict],
                  lead: dict | None = None) -> None:
    """Write one prior-digest line to a JSONL archive, in the shape archive.py emits."""
    record = {
        "date": day.isoformat(),
        "email_subject": "prior subject",
        "intro": "prior intro",
        "lead_brief": lead,
        "briefs": briefs,
        "topic_tags": tags,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _prompt_sent_to_model(client: _FakeClient) -> str:
    """The user-message text the synthesizer handed the client."""
    kwargs = client.messages.last_kwargs
    assert kwargs is not None, "model was never called"
    return kwargs["messages"][0]["content"]


def test_synthesize_builds_thread_context_from_archive(article_factory, tmp_path):
    # Seed a recent 'ASML High-NA' thread the way archive.append_digest would.
    archive_path = tmp_path / "digest_archive.jsonl"
    recent_day = datetime.now(UTC).date() - timedelta(days=3)
    _seed_archive(
        archive_path,
        day=recent_day,
        tags=["ASML High-NA", "BIS export rules"],
        lead={
            "headline": "ASML ships its first High-NA EUV scanner",
            "paragraph": (
                "**ASML** shipped its first High-NA EUV system to a US lab. "
                "Numerical aperture rises to 0.55."
            ),
            "category": "company",
            "citations": [{"source": "SemiWiki", "url": "https://example.com/highna"}],
        },
        briefs=[
            {
                "headline": "BIS widens export controls",
                "paragraph": "The **Bureau of Industry and Security** added line items.",
                "category": "policy",
                "citations": [{"source": "FR", "url": "https://example.com/bis"}],
            }
        ],
    )

    client = _FakeClient(_response())
    synthesize(
        _ranked(article_factory),
        criteria_path=CRITERIA,
        total_fetched=3,
        client=client,
        archive_path=archive_path,
    )

    prompt = _prompt_sent_to_model(client)
    # The previously-reported block is present and instructs the UPDATE behavior.
    assert "PREVIOUSLY REPORTED" in prompt
    assert "THE UPDATE" in prompt
    # Prior topic tags and a prior headline both made it into the context.
    assert "ASML High-NA" in prompt
    assert "ASML ships its first High-NA EUV scanner" in prompt
    # The lead's first-sentence gist is included, with bold markers stripped.
    assert "shipped its first High-NA EUV system" in prompt
    assert "**ASML**" not in prompt.split("=== candidates begin ===")[0]


def test_synthesize_cold_start_has_no_thread_block(article_factory, tmp_path):
    # Missing archive -> no thread block, no crash, prompt is exactly today's.
    client = _FakeClient(_response())
    synthesize(
        _ranked(article_factory),
        criteria_path=CRITERIA,
        total_fetched=3,
        client=client,
        archive_path=tmp_path / "does_not_exist.jsonl",
    )

    prompt = _prompt_sent_to_model(client)
    assert "PREVIOUSLY REPORTED" not in prompt
    assert "THE UPDATE" not in prompt


def test_synthesize_empty_archive_has_no_thread_block(article_factory, tmp_path):
    archive_path = tmp_path / "digest_archive.jsonl"
    archive_path.write_text("", encoding="utf-8")

    client = _FakeClient(_response())
    synthesize(
        _ranked(article_factory),
        criteria_path=CRITERIA,
        total_fetched=3,
        client=client,
        archive_path=archive_path,
    )

    prompt = _prompt_sent_to_model(client)
    assert "PREVIOUSLY REPORTED" not in prompt


def test_synthesize_thread_context_drops_stale_days(article_factory, tmp_path):
    # A digest older than the threading window must NOT appear in the block.
    archive_path = tmp_path / "digest_archive.jsonl"
    stale_day = datetime.now(UTC).date() - timedelta(days=40)
    _seed_archive(
        archive_path,
        day=stale_day,
        tags=["Ancient thread"],
        briefs=[
            {
                "headline": "Old news nobody should re-thread",
                "paragraph": "Something that happened over a month ago.",
                "category": "company",
                "citations": [{"source": "X", "url": "https://example.com/old"}],
            }
        ],
    )

    client = _FakeClient(_response())
    synthesize(
        _ranked(article_factory),
        criteria_path=CRITERIA,
        total_fetched=3,
        client=client,
        archive_path=archive_path,
    )

    prompt = _prompt_sent_to_model(client)
    # read_recent's window filtering means the stale day yields no records.
    assert "PREVIOUSLY REPORTED" not in prompt
    assert "Ancient thread" not in prompt
