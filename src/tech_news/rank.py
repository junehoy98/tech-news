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

# Articles are scored in batches rather than one giant call. A single
# "return EXACTLY N entries" request gets unreliable as N grows — the model
# truncates (overflowing max_tokens → no parsed output → empty digest) or
# silently drops fingerprints. Smaller batches keep each call well within the
# output cap and localize any failure to one batch.
#
# The rubric is sent as a cache_control system block (see _score_batch). NOTE:
# at its current ~2.5K tokens it sits below Haiku 4.5's effective minimum
# cacheable prefix, so caching is a no-op today and the savings would be
# negligible regardless (a small rubric reused across ~4 cheap Haiku calls).
# The marker is kept because it's harmless and engages automatically if the
# rubric ever grows past the threshold.
RANKING_BATCH_SIZE = 40

# Per batch of RANKING_BATCH_SIZE, output JSON is ~2-3K tokens. 16K is ample
# headroom so a verbose batch can't truncate.
MAX_RANKING_TOKENS = 16000


Category = Literal["company", "tech", "policy", "business", "opinion"]


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
    """Score every article in batches; return them sorted by score desc.

    A failed batch (no parsed output) is logged and skipped rather than
    sinking the whole run. Any article the model omits from a batch is kept
    with a default score of 0 so it's counted, not silently lost.
    """
    if not articles:
        return []

    client = client or anthropic.Anthropic()
    rubric = criteria_path.read_text(encoding="utf-8")
    by_fp = {a.fingerprint: a for a in articles}

    batches = [
        articles[i : i + RANKING_BATCH_SIZE]
        for i in range(0, len(articles), RANKING_BATCH_SIZE)
    ]
    ranked: list[RankedArticle] = []
    for n, batch in enumerate(batches, start=1):
        items = _score_batch(batch, rubric, client, n, len(batches))

        scored_fps: set[str] = set()
        for item in items:
            article = by_fp.get(item.fingerprint)
            if article is None:
                log.warning("Haiku returned unknown fingerprint %s; skipping", item.fingerprint)
                continue
            scored_fps.add(item.fingerprint)
            ranked.append(
                RankedArticle(
                    article=article,
                    score=item.score,
                    category=item.category,
                    topic_tag=item.topic_tag,
                )
            )

        for article in batch:
            if article.fingerprint not in scored_fps:
                log.warning(
                    "Haiku omitted %s (%r); defaulting to score 0",
                    article.fingerprint, article.title[:60],
                )
                ranked.append(
                    RankedArticle(
                        article=article,
                        score=0,
                        category=article.category,
                        topic_tag="",
                    )
                )

    ranked.sort(key=lambda r: (-r.score, r.article.priority))
    return ranked


def _score_batch(
    batch: list[Article],
    rubric: str,
    client: anthropic.Anthropic,
    idx: int,
    total: int,
) -> list[ItemScore]:
    """Score one batch. Returns [] (logged) on a failed/empty parse."""
    response = client.messages.parse(
        model=RANKING_MODEL,
        max_tokens=MAX_RANKING_TOKENS,
        # List-of-blocks form so the rubric can carry cache_control. Engages
        # only once the cached prefix clears Haiku's minimum (see RANKING_BATCH_SIZE
        # note); harmless and a no-op below that.
        system=[{"type": "text", "text": rubric, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _format_articles(batch)}],
        output_format=RankingResponse,
    )
    _log_usage(response, idx, total)

    parsed = response.parsed_output
    if parsed is None:
        log.error(
            "Haiku ranking batch %d/%d returned no parsed output; stop_reason=%s",
            idx, total, response.stop_reason,
        )
        return []
    return parsed.items


def _log_usage(response: object, idx: int, total: int) -> None:
    """Log token usage, including cache hits, so cost and caching are visible."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    log.info(
        "Rank batch %d/%d tokens: in=%s out=%s cache_write=%s cache_read=%s",
        idx, total,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
        getattr(usage, "cache_creation_input_tokens", 0),
        getattr(usage, "cache_read_input_tokens", 0),
    )


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
