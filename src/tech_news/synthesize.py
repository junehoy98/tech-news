"""Second pass: Sonnet takes scored articles and writes clustered briefs.

Most briefs are a 60-100 word paragraph that may draw on multiple source
articles covering the same news event. The single most important story is
written as a longer LEAD brief (~120-180 words) that carries the optics /
metrology / lithography technical read a physics reader wants. Sonnet also
produces a one-line email subject and a short intro. The output is a Digest
object.

This is where the editorial voice and length discipline live. The rubric
in config/criteria.md drives both passes — this one just gets a focused
synthesis instruction on top.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import anthropic
from pydantic import BaseModel, Field

from .rank import LLM_MAX_RETRIES, RANKING_MODEL, RankedArticle

log = logging.getLogger(__name__)

SYNTHESIS_MODEL = "claude-sonnet-4-6"
# Bigger budget: the digest now runs longer (one ~120-180 word lead brief plus
# 6-8 regular briefs), so the previous 4000-token cap risked truncation.
SYNTHESIS_MAX_TOKENS = 7000

# Only items at or above this score are sent to the synthesizer. Lower than
# the previous threshold because the synthesizer may use multiple weaker
# articles to corroborate one strong story.
MIN_SCORE_FOR_SYNTHESIS = 6

# How many candidate articles the synthesizer sees. The model picks among
# them and writes the lead plus the regular briefs.
CANDIDATE_POOL_SIZE = 55

# Per-candidate body budget. The synthesizer sees far fewer items than the
# ranker, so it can afford a longer slice of each article's fetched body to
# write from — but still bounded so the whole candidate block stays compact.
SYNTH_BODY_CHARS = 1500

# The top few candidates get a deeper slice: the lead brief is written from
# these, and the grounding rule below forbids numbers not present in the
# excerpt, so the likely-lead sources need enough text to carry real figures.
SYNTH_DEEP_BODY_CHARS = 3000
SYNTH_DEEP_CANDIDATES = 10

DEFAULT_TARGET_BRIEFS = 7

# Article kinds that are PRIMARY sources — a company's own press release
# ("scrape") or an SEC filing ("edgar"), plus regulatory filings
# ("federal_register"). These get a visible marker on their source line so the
# synthesizer knows to cite the official/primary link alongside the trade press.
_PRIMARY_KINDS = frozenset({"scrape", "edgar", "federal_register"})

# How far back the storyline-threading context looks. The synthesizer is told
# what we reported over roughly the last week and a half so it can write the
# UPDATE on a continuing story instead of re-introducing it cold. Kept short:
# threads older than this have usually gone quiet, and a wider window only
# bloats the prompt.
THREAD_CONTEXT_DAYS = 10

# Hard cap on how many prior briefs feed the "previously reported" block, so a
# run of busy days can't balloon the synthesis prompt. Most recent days first.
MAX_THREAD_BRIEFS = 24

# Same idea for the "Prior topic tags" line: newest-first, bounded, so the tag
# list can never balloon the prompt the way the pre-fix all-ranked harvest did.
MAX_THREAD_TAGS = 40

# Default archive location, mirroring main.py's data/digest_archive.jsonl under
# the project root. Injectable so tests can point at a seeded fixture (or skip
# threading entirely with a nonexistent path).
DEFAULT_ARCHIVE_PATH = Path(__file__).resolve().parents[2] / "data" / "digest_archive.jsonl"


class TagGroup(BaseModel):
    canonical: str = Field(description="The canonical tag for this news event")
    variants: list[str] = Field(
        description="Every input tag (verbatim) that refers to this same event"
    )


class TagConsolidation(BaseModel):
    groups: list[TagGroup] = Field(
        description="Groups of input tags that refer to the same news event"
    )


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
    category: str = Field(description="company | tech | policy | business | opinion")


class LeadBrief(Brief):
    """The single most important story, written longer and deeper.

    Same shape as a regular Brief (so the template and archive can render it
    identically), but the paragraph runs ~120-180 words and carries the
    optics / metrology / lithography technical read a physics reader wants.
    """

    paragraph: str = Field(
        description=(
            "Single paragraph of 120-180 words on the day's most important "
            "story. Same plain American English voice and bolding rules as a "
            "regular brief — NOT British/Economist style, NO 'why this "
            "matters' / study-guide tail. The extra length goes to the "
            "TECHNICAL read a physics/optics reader wants: when the story "
            "touches optics, metrology, or lithography, explain what the "
            "optical or metrology change actually IS in concrete physical "
            "terms — numerical aperture and how it sets resolution, "
            "wavelength, illumination or pupil shaping, overlay/alignment "
            "error budgets in nanometers, the sensor or interferometer "
            "principle, the resist or mask physics. Define semi-industry "
            "jargon in tight 3-7 word parentheticals on first use; the reader "
            "knows physics but not industry shorthand. If the lead story has "
            "no optics/metrology/lithography angle, still run long but spend "
            "the words on the substantive technical or business mechanics — "
            "do not pad. Bold the same TWO things as a regular brief (first "
            "entity mention + one punchline); the longer length does not mean "
            "more bolds."
        )
    )


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
    lead_brief: LeadBrief = Field(
        description=(
            "The single most important story of the day, written as a longer "
            "(120-180 word) brief with the technical depth described above. "
            "Exactly one; do not also include this story in `briefs`."
        )
    )
    briefs: list[Brief] = Field(
        description=(
            "The remaining 6-8 briefs covering the rest of the day's news, "
            "ordered by importance. Each is 60-100 words. Does NOT repeat the "
            "lead story."
        )
    )


@dataclass
class Digest:
    date: date
    email_subject: str
    intro: str
    lead_brief: Brief | None
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
    archive_path: Path = DEFAULT_ARCHIVE_PATH,
    digest_date: date | None = None,
) -> Digest:
    """Cluster top-ranked items into a small set of briefs.

    `archive_path` points at the per-day digest archive; recent digests are
    folded into a "previously reported" block so the model writes the UPDATE on
    a continuing story rather than re-introducing it cold. A missing or empty
    archive (cold start) yields no thread block and behaves exactly as before.

    `digest_date` is the date stamped on the digest; the caller passes the
    date in the SEND timezone (a UTC runner's date.today() can be tomorrow
    relative to an 08:58 America/New_York send). Defaults to local today.
    """
    digest_date = digest_date or date.today()
    if not ranked:
        return _empty_digest(total_fetched, digest_date)

    candidates = [r for r in ranked if r.score >= MIN_SCORE_FOR_SYNTHESIS][:CANDIDATE_POOL_SIZE]
    if not candidates:
        return _empty_digest(total_fetched, digest_date)

    # An APIError here (after SDK retries) propagates on purpose: main()'s
    # guard emails a failure alert, and the next backup scheduled run retries
    # the whole day since the sent-marker was never written.
    client = client or anthropic.Anthropic(max_retries=LLM_MAX_RETRIES)
    _consolidate_tags(candidates, client)
    rubric = criteria_path.read_text(encoding="utf-8")
    thread_block = _build_thread_context(archive_path)
    user_message = _format_candidates(candidates, target_briefs, thread_block)

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
        return _empty_digest(total_fetched, digest_date)

    _clean_citations(parsed, candidates)

    lead_words = len(parsed.lead_brief.paragraph.split()) if parsed.lead_brief else 0
    if lead_words and not (100 <= lead_words <= 190):
        log.warning("Lead brief is %d words (target 120-180)", lead_words)

    return Digest(
        date=digest_date,
        email_subject=parsed.email_subject,
        intro=parsed.intro,
        lead_brief=parsed.lead_brief,
        briefs=list(parsed.briefs),
        total_kept=len(candidates),
        total_fetched=total_fetched,
    )


def _empty_digest(total_fetched: int, digest_date: date | None = None) -> Digest:
    return Digest(
        date=digest_date or date.today(),
        email_subject="Semi Daily — no qualifying news",
        intro="Nothing crossed today's relevance threshold.",
        lead_brief=None,
        briefs=[],
        total_kept=0,
        total_fetched=total_fetched,
    )


def _consolidate_tags(candidates: list[RankedArticle], client: anthropic.Anthropic) -> None:
    """Merge topic_tags that name the same news event, across ranking batches.

    The ranker scores in independent batches of 40 that never see each other's
    tags, so the same story routinely gets near-duplicate tags ("ASML High-NA
    ship" vs "ASML ships High-NA") and the synthesizer's tag-equality
    clustering falls apart (~175 distinct tags for one day was observed). One
    cheap Haiku call over just the candidates' tags maps variants onto a
    canonical form. Best-effort: any failure leaves tags untouched.
    """
    tags = sorted({r.topic_tag.strip() for r in candidates if r.topic_tag.strip()})
    if len(tags) < 2:
        return
    prompt = (
        "The following topic tags were assigned to news articles by independent "
        "passes that could not see each other's output, so the SAME news event "
        "may appear under several near-duplicate tags. Group tags that refer to "
        "the same news event; echo each input tag verbatim in exactly one "
        "group's `variants`. Do NOT merge tags about different events, even for "
        "the same company. A tag with no duplicates gets its own group.\n\n"
        + "\n".join(f"- {t}" for t in tags)
    )
    try:
        response = client.messages.parse(
            model=RANKING_MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
            output_format=TagConsolidation,
        )
    except anthropic.APIError as e:
        log.warning("Tag consolidation failed (%s); keeping raw tags", e)
        return
    parsed = response.parsed_output
    if parsed is None:
        log.warning("Tag consolidation returned no parsed output; keeping raw tags")
        return

    try:
        mapping = {
            v.strip().casefold(): g.canonical.strip()
            for g in parsed.groups
            for v in g.variants
            if v.strip() and g.canonical.strip()
        }
    except Exception as e:  # noqa: BLE001 — best-effort; malformed output keeps raw tags
        log.warning("Tag consolidation output unusable (%s); keeping raw tags", e)
        return
    if not mapping:
        return
    merged = 0
    for r in candidates:
        canon = mapping.get(r.topic_tag.strip().casefold())
        if canon and canon != r.topic_tag:
            r.topic_tag = canon
            merged += 1
    log.info(
        "Tag consolidation: %d tags -> %d canonical groups (%d articles remapped)",
        len(tags), len(parsed.groups), merged,
    )


def _clean_citations(parsed: SynthesisResponse, candidates: list[RankedArticle]) -> None:
    """Dedupe each brief's citations and drop any URL not in the candidate set.

    The prompt forbids invented citations, but nothing used to enforce it, and
    duplicate citations ("Tom's Hardware, Tom's Hardware") shipped in practice.
    Mutates the parsed briefs in place. A brief whose citations ALL turn out
    invalid keeps an empty list — the template tolerates it — and logs loudly,
    because that brief is unsourced prose.

    URLs are compared NORMALIZED (host case, trailing slash, tracking params,
    fragment) because feeds ship tracking-tagged URLs (?utm_source=rss...) and
    the model cites the clean canonical form — an exact match would drop that
    legitimate citation.
    """
    valid_urls = {_normalize_url(r.article.url) for r in candidates}
    briefs = ([parsed.lead_brief] if parsed.lead_brief else []) + list(parsed.briefs)
    for b in briefs:
        seen: set[str] = set()
        kept: list[Citation] = []
        for c in b.citations:
            norm = _normalize_url(c.url)
            if norm in seen:
                continue
            seen.add(norm)
            if norm not in valid_urls:
                log.warning(
                    "Dropping citation not in candidate set: %r (%s) on brief %r",
                    c.source, c.url, b.headline,
                )
                continue
            kept.append(c)
        if not kept and b.citations:
            log.error("Brief %r has no valid citations left after cleaning", b.headline)
        b.citations = kept


def _normalize_url(url: str) -> str:
    """Canonical form for citation matching: lowercase host, no trailing slash,
    no fragment, tracking query params (utm_*, fbclid, gclid) stripped."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip()
    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and not pair.lower().startswith(("utm_", "fbclid", "gclid"))
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, "")
    )


def _build_thread_context(archive_path: Path) -> str:
    """Build the bounded "PREVIOUSLY REPORTED" block from the recent archive.

    Reads the last THREAD_CONTEXT_DAYS of sent digests and distills them into a
    compact reminder of what we already covered: the distinct topic_tags plus a
    short headline/gist line per brief (lead first). The synthesizer uses this
    to write the UPDATE on a continuing thread instead of re-explaining settled
    background.

    Returns "" on a cold start (missing/empty archive) or any read trouble, so
    the synthesis prompt is byte-for-byte today's when there's no history. The
    archive import is deferred to avoid a circular import (archive imports
    Digest from this module).
    """
    try:
        from . import archive

        recent = archive.read_recent(archive_path, days=THREAD_CONTEXT_DAYS)
    except Exception:  # noqa: BLE001 — threading is best-effort; never block a run
        log.exception("Failed to read recent archive for threading; skipping thread block")
        return ""

    if not recent:
        return ""

    # Newest day first so the freshest context leads and the brief budget is
    # spent on the most relevant threads.
    tags: list[str] = []
    brief_lines: list[str] = []
    for record in reversed(recent):
        date_str = record.get("date", "?")
        for tag in record.get("topic_tags", []) or []:
            if len(tags) >= MAX_THREAD_TAGS:
                break
            if tag and tag not in tags:
                tags.append(tag)

        # Lead first, then the regular briefs for that day.
        day_briefs = []
        lead = record.get("lead_brief")
        if lead:
            day_briefs.append(lead)
        day_briefs.extend(record.get("briefs", []) or [])

        for b in day_briefs:
            if len(brief_lines) >= MAX_THREAD_BRIEFS:
                break
            headline = (b.get("headline") or "").strip()
            if not headline:
                continue
            gist = _first_sentence(b.get("paragraph") or "")
            line = f"- ({date_str}) {headline}"
            if gist:
                line += f" — {gist}"
            brief_lines.append(line)
        if len(brief_lines) >= MAX_THREAD_BRIEFS:
            break

    if not brief_lines:
        return ""

    parts = [
        "=== PREVIOUSLY REPORTED (last ~10 days) begin ===",
        "",
        "Below is what this digest already covered recently. For any candidate "
        "today that CONTINUES one of these threads (its topic_tag matches or is "
        "clearly related to a prior story), write THE UPDATE — the new "
        "development and what changed since (e.g. 'a third tool shipped, after "
        "last week's two') — and do NOT re-explain background the reader already "
        "got. For genuinely new stories with no thread below, write as you "
        "normally would.",
        "",
    ]
    if tags:
        parts.append("Prior topic tags: " + ", ".join(tags))
        parts.append("")
    parts.append("Prior briefs (newest first):")
    parts.extend(brief_lines)
    parts.append("")
    parts.append("=== PREVIOUSLY REPORTED end ===")
    return "\n".join(parts)


def _first_sentence(text: str, max_chars: int = 160) -> str:
    """A short, plain gist for the thread block: first sentence of a brief.

    Strips markdown bold markers, takes up to the first sentence terminator, and
    hard-caps the length so the "previously reported" block stays compact.
    """
    plain = text.replace("**", "").strip()
    if not plain:
        return ""
    for end in (". ", ".\n", "; "):
        idx = plain.find(end)
        if 0 <= idx <= max_chars:
            return plain[: idx + 1].strip()
    if len(plain) <= max_chars:
        return plain
    return plain[:max_chars].rstrip() + "…"


def _format_candidates(
    candidates: list[RankedArticle],
    target_briefs: int,
    thread_block: str = "",
) -> str:
    lines = [
        f"You have {len(candidates)} scored articles below. Synthesize them into "
        f"one LEAD brief plus {target_briefs} (6-8 acceptable) regular briefs for "
        "the daily digest.",
        "",
        "Clustering: articles sharing the same topic_tag cover the same event — "
        "combine them into ONE brief that cites all relevant sources. Articles "
        "with distinct topic_tags but a clear thematic link (e.g. multiple "
        "items on export-control policy) may also be combined into one brief.",
        "",
        "Selection: not every article needs to appear. Drop weak or redundant ones. "
        "Aim for breadth across categories (company / tech / policy / business / opinion) "
        "when the news supports it.",
        "",
        "Primary sources: a candidate whose source line is marked [PRIMARY SOURCE] is "
        "the official/primary record — a company's own press release or an SEC / "
        "regulatory filing. When clustering an event, if a [PRIMARY SOURCE] candidate "
        "covers it, INCLUDE its citation alongside the trade-press citations, listed "
        "FIRST; readers value the official link. Only cite primary candidates ACTUALLY present in the "
        "list below — never invent a citation. This does not change the prose voice or "
        "the bolding rules.",
        "",
        "LEAD brief: pick the single most important story of the day and write it "
        "as the `lead_brief` — a longer 120-180 word paragraph. Spend the extra "
        "length on the TECHNICAL read a physics/optics reader wants: when the "
        "story touches optics, metrology, or lithography, explain what the optical "
        "or metrology change actually IS in concrete physical terms (numerical "
        "aperture and the resolution it buys, wavelength, illumination/pupil "
        "shaping, overlay error budgets in nanometers, the sensor or "
        "interferometer principle, resist or mask physics). GROUNDING: include a "
        "specific figure (NA, wavelength, nm, dollars, percent) ONLY if it appears "
        "in the candidate text below — when the excerpt lacks the number, explain "
        "the mechanism qualitatively instead. NEVER supply figures from memory. "
        "If the lead has no "
        "optics/metrology/lithography angle, still run long but on the real "
        "technical or business mechanics — never pad. Same voice and SAME bolding "
        "discipline as a regular brief (two bolds: first entity + one punchline); "
        "longer does NOT mean more bolds. Do NOT also place the lead story in "
        "`briefs`.",
        "",
        "Each regular brief: a single 60-100 word paragraph in plain American "
        "English, with citations to every article it draws on. Use **markdown "
        "bold** SPARINGLY — emphasize the most important entity or fact, not every "
        "company name. Vary sentence openers across briefs so the prose doesn't "
        "read as a template.",
        "",
        "Also produce: a one-line email_subject weaving the 2-3 biggest stories, "
        "and a 25-45 word intro that frames the day. No 'why this matters' / "
        "study-guide framing anywhere. The reader knows why they're reading.",
        "",
    ]
    # Storyline threading: when there's recent history, drop the "previously
    # reported" block in just ahead of today's candidates so the model can write
    # the UPDATE on a continuing thread. Cold start leaves this empty -> the
    # prompt is exactly today's.
    if thread_block:
        lines.append(thread_block)
        lines.append("")
    lines += [
        "=== candidates begin ===",
        "",
    ]
    for i, r in enumerate(candidates):
        body_budget = SYNTH_DEEP_BODY_CHARS if i < SYNTH_DEEP_CANDIDATES else SYNTH_BODY_CHARS
        lines.append(f"score: {r.score}  category: {r.category}  topic_tag: {r.topic_tag}")
        # Mark primary sources (own press release / SEC / regulatory filing) so the
        # model knows to cite the official link alongside the trade press.
        primary_marker = " [PRIMARY SOURCE]" if r.article.kind in _PRIMARY_KINDS else ""
        lines.append(f"source: {r.article.source_name}{primary_marker}")
        lines.append(f"title: {r.article.title}")
        # Prefer the fetched body (real reporting to write from) over the teaser;
        # fall back to the summary when full text wasn't available. Both are
        # trimmed to keep the candidate block compact.
        if r.article.body:
            lines.append(f"body: {r.article.body[:body_budget]}")
        elif r.article.summary:
            lines.append(f"summary: {r.article.summary[:500]}")
        lines.append(f"url: {r.article.url}")
        lines.append("")
    lines.append("=== candidates end ===")
    return "\n".join(lines)
