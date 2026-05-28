"""Second pass: Sonnet takes scored articles and writes clustered briefs.

Each brief is a 60-100 word paragraph that may draw on multiple source
articles covering the same news event. Sonnet also produces a one-line
email subject and a short intro. The output is a Digest object.

This is where the editorial voice and length discipline live. The rubric
in config/criteria.md drives both passes — this one just gets a focused
synthesis instruction on top.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from .rank import RankedArticle

log = logging.getLogger(__name__)

SYNTHESIS_MODEL = "claude-sonnet-4-6"
SYNTHESIS_MAX_TOKENS = 4000

# Only items at or above this score are sent to the synthesizer. Lower than
# the previous threshold because the synthesizer may use multiple weaker
# articles to corroborate one strong story.
MIN_SCORE_FOR_SYNTHESIS = 6

# How many candidate articles the synthesizer sees. The model picks among
# them and writes 4-6 briefs total.
CANDIDATE_POOL_SIZE = 40

DEFAULT_TARGET_BRIEFS = 5


class Citation(BaseModel):
    source: str = Field(description="Source name to display (e.g. 'SemiWiki')")
    url: str = Field(description="URL of the cited article")


class Brief(BaseModel):
    headline: str = Field(
        description=(
            "One-line headline for this brief, 6-12 words, plain news voice. "
            "No bold markup in the headline."
        )
    )
    paragraph: str = Field(
        description=(
            "Single paragraph of 60-100 words drawing on the cited articles. "
            "American English, plain professional voice — NOT British/Economist "
            "style (no 'bullish', 'knock-on', 'organised', 'extraterritorial "
            "overreach', etc). "
            "BOLDING (use **markdown bold**): bold TWO things per brief "
            "(max 3). (A) The first mention of each major company or "
            "institution in the brief body (skip if already in the headline) "
            "— e.g. **ASML**, **KLA**, **TSMC**, **Imec**, the **Dutch "
            "government**. Only the first mention; never re-bold the same "
            "entity. (B) The punchline: a 2-5 word phrase marking a number, "
            "a state change, or an unexpected angle (e.g. **$2 billion**, "
            "**moves into mass production**, **without EUV**). DON'T bold: "
            "jargon being defined, parenthetical definitions, generic noun "
            "phrases, or anything over ~5 words. See the rubric for the "
            "scannable-skeleton test and full examples. "
            "Vary sentence openers across briefs. Define semi-industry "
            "jargon in 3-7 word parentheticals on first use. "
            "No 'why this matters' / study-guide tail."
        )
    )
    citations: list[Citation] = Field(
        description="The articles this brief draws on; at least one."
    )
    category: str = Field(description="company | tech | policy | business")


class SynthesisResponse(BaseModel):
    email_subject: str = Field(
        description=(
            "Single line, 60-80 chars, weaving the 2-3 biggest stories into "
            "a real news subject. E.g. 'ASML High-NA ships; Dutch push back on "
            "US export rules; TSMC adds $11B in Arizona'. NO date suffix "
            "(template adds the date). Plain news voice."
        )
    )
    intro: str = Field(
        description=(
            "1-2 sentence editor's note, 25-45 words, plain American English. "
            "Frames the day in one line and previews the dominant thread. NO "
            "'today's digest covers...' preamble; just open with the news. "
            "No study-guide framing. Empty string is acceptable if the "
            "subject line already conveys the framing."
        )
    )
    briefs: list[Brief] = Field(
        description="4-6 briefs covering the day's news, ordered by importance."
    )


@dataclass
class Digest:
    date: date
    email_subject: str
    intro: str
    briefs: list[Brief]
    total_kept: int
    total_fetched: int

    @property
    def date_short(self) -> str:
        return f"{self.date.strftime('%b')} {self.date.day}, {self.date.year}"

    @property
    def date_long(self) -> str:
        return f"{self.date.strftime('%A')}, {self.date.strftime('%B')} {self.date.day}, {self.date.year}"


def synthesize(
    ranked: list[RankedArticle],
    criteria_path: Path,
    total_fetched: int,
    client: anthropic.Anthropic | None = None,
    target_briefs: int = DEFAULT_TARGET_BRIEFS,
) -> Digest:
    """Cluster top-ranked items into a small set of briefs."""
    if not ranked:
        return _empty_digest(total_fetched)

    candidates = [r for r in ranked if r.score >= MIN_SCORE_FOR_SYNTHESIS][:CANDIDATE_POOL_SIZE]
    if not candidates:
        return _empty_digest(total_fetched)

    client = client or anthropic.Anthropic()
    rubric = criteria_path.read_text(encoding="utf-8")
    user_message = _format_candidates(candidates, target_briefs)

    response = client.messages.parse(
        model=SYNTHESIS_MODEL,
        max_tokens=SYNTHESIS_MAX_TOKENS,
        system=rubric,
        messages=[{"role": "user", "content": user_message}],
        output_format=SynthesisResponse,
    )

    parsed = response.parsed_output
    if parsed is None:
        log.error("Synthesis returned no parsed output; stop_reason=%s", response.stop_reason)
        return _empty_digest(total_fetched)

    return Digest(
        date=date.today(),
        email_subject=parsed.email_subject,
        intro=parsed.intro,
        briefs=list(parsed.briefs),
        total_kept=len(candidates),
        total_fetched=total_fetched,
    )


def _empty_digest(total_fetched: int) -> Digest:
    return Digest(
        date=date.today(),
        email_subject="Semi Daily — no qualifying news",
        intro="Nothing crossed today's relevance threshold.",
        briefs=[],
        total_kept=0,
        total_fetched=total_fetched,
    )


def _format_candidates(candidates: list[RankedArticle], target_briefs: int) -> str:
    lines = [
        f"You have {len(candidates)} scored articles below. Synthesize them into "
        f"{target_briefs} (4-6 acceptable) briefs for the daily digest.",
        "",
        "Clustering: articles sharing the same topic_tag cover the same event — "
        "combine them into ONE brief that cites all relevant sources. Articles "
        "with distinct topic_tags but a clear thematic link (e.g. multiple "
        "items on export-control policy) may also be combined into one brief.",
        "",
        "Selection: not every article needs to appear. Drop weak or redundant ones. "
        "Aim for breadth across categories (company / tech / policy / business) "
        "when the news supports it.",
        "",
        "Each brief: a single 60-100 word paragraph in plain American English, "
        "with citations to every article it draws on. Use **markdown bold** "
        "SPARINGLY — emphasize the most important entity or fact, not every "
        "company name. Vary sentence openers across briefs so the prose doesn't "
        "read as a template.",
        "",
        "Also produce: a one-line email_subject weaving the 2-3 biggest stories, "
        "and a 25-45 word intro that frames the day. No 'why this matters' / "
        "study-guide framing anywhere. The reader knows why they're reading.",
        "",
        "=== candidates begin ===",
        "",
    ]
    for r in candidates:
        lines.append(f"score: {r.score}  category: {r.category}  topic_tag: {r.topic_tag}")
        lines.append(f"source: {r.article.source_name}")
        lines.append(f"title: {r.article.title}")
        if r.article.summary:
            # Trim long summaries to keep input compact.
            snip = r.article.summary[:500]
            lines.append(f"summary: {snip}")
        lines.append(f"url: {r.article.url}")
        lines.append("")
    lines.append("=== candidates end ===")
    return "\n".join(lines)
