"""Score articles 0-10 for digest relevance — first of two LLM passes.

Haiku 4.5 scores every fetched article using the rubric in config/criteria.md
as the system prompt. No prose written here; that's the synthesize step.
Splitting the work means the cheap fast model does the filtering, then the
better model writes the prose for only the top candidates — cheaper and less
hallucination surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .sources import Article

log = logging.getLogger(__name__)

# Haiku 4.5: fast and cheap; the scoring rubric doesn't need a frontier model.
RANKING_MODEL = "claude-haiku-4-5"

# Each ItemScore serializes to ~130-150 chars; at 200+ articles output JSON
# can run 8K+ tokens. Give Haiku plenty of headroom.
MAX_RANKING_TOKENS = 16000


Category = Literal["company", "tech", "policy", "business"]


class ItemScore(BaseModel):
    fingerprint: str = Field(description="The article's fingerprint, echoed back verbatim")
    score: int = Field(ge=0, le=10, description="Relevance score per the rubric")
    category: Category = Field(description="Editorial category")
    topic_tag: str = Field(
        description=(
            "Short 1-4 word tag identifying the news event this article covers, "
            "e.g. 'ASML High-NA shipment', 'Dutch export pushback', 'TSMC Arizona "
            "capex'. Articles covering the SAME event must share IDENTICAL tags "
            "(verbatim) so the synthesizer can cluster them. Be terse and consistent."
        )
    )


class RankingResponse(BaseModel):
    items: list[ItemScore]


@dataclass
class RankedArticle:
    article: Article
    score: int
    category: str
    topic_tag: str


def rank_articles(
    articles: list[Article],
    criteria_path: Path,
    client: anthropic.Anthropic | None = None,
) -> list[RankedArticle]:
    """Score every article; return them sorted by score desc."""
    if not articles:
        return []

    client = client or anthropic.Anthropic()
    rubric = criteria_path.read_text(encoding="utf-8")

    user_message = _format_articles(articles)

    response = client.messages.parse(
        model=RANKING_MODEL,
        max_tokens=MAX_RANKING_TOKENS,
        system=rubric,
        messages=[{"role": "user", "content": user_message}],
        output_format=RankingResponse,
    )

    parsed = response.parsed_output
    if parsed is None:
        log.error("Haiku ranking returned no parsed output; stop_reason=%s", response.stop_reason)
        return []

    by_fp = {a.fingerprint: a for a in articles}
    ranked: list[RankedArticle] = []
    for item in parsed.items:
        article = by_fp.get(item.fingerprint)
        if article is None:
            log.warning("Haiku returned unknown fingerprint %s; skipping", item.fingerprint)
            continue
        ranked.append(
            RankedArticle(
                article=article,
                score=item.score,
                category=item.category,
                topic_tag=item.topic_tag,
            )
        )

    ranked.sort(key=lambda r: (-r.score, r.article.priority))
    return ranked


def _format_articles(articles: list[Article]) -> str:
    n = len(articles)
    lines = [
        f"Below are {n} articles fetched from RSS feeds today.",
        "",
        f"Return EXACTLY {n} ItemScore entries — one per article, no more, no less.",
        "Echo each article's fingerprint verbatim; do NOT invent fingerprints.",
        "Do NOT continue past the articles given.",
        "",
        "For each: score 0-10 per the rubric, pick a category, and assign a SHORT",
        "topic_tag (1-4 words). The topic_tag is critical: articles covering the",
        "SAME news event must share the IDENTICAL tag (e.g. three sources",
        "reporting ASML High-NA shipment all get tag 'ASML High-NA shipment').",
        "Be consistent — same words, same casing.",
        "",
        "=== articles begin ===",
        "",
    ]
    for a in articles:
        lines.append(f"fingerprint: {a.fingerprint}")
        lines.append(f"source: {a.source_name} (suggested category: {a.category})")
        lines.append(f"title: {a.title}")
        if a.summary:
            lines.append(f"summary: {a.summary}")
        lines.append("")
    lines.append("=== articles end ===")
    lines.append("")
    lines.append(f"Reminder: exactly {n} ItemScore entries, identical topic_tag for same event.")
    return "\n".join(lines)
