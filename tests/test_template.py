from datetime import date
from pathlib import Path

from tech_news.mailer import render_html
from tech_news.synthesize import Brief, Citation, Digest


def test_template_renders_briefs_with_multiple_citations():
    briefs = [
        Brief(
            headline="ASML signals High-NA EUV is ready for commercial production",
            paragraph=(
                "**ASML** declared its next-generation lithography (chip-printing) tools "
                "ready for mass production, with the first commercial shipments expected "
                "within months. The milestone matters because High-NA EUV enables features "
                "below 14 nm half-pitch, the technology requirement for the next chip "
                "generation. KLA and Onto Innovation now face an accelerated metrology "
                "qualification timeline."
            ),
            citations=[
                Citation(source="SemiWiki", url="https://example.com/semiwiki/asml"),
                Citation(source="Bits & Chips", url="https://example.com/bitschips/asml"),
            ],
            category="tech",
        ),
        Brief(
            headline="Dutch government pushes back on US export legislation",
            paragraph=(
                "The Netherlands formally objected to a proposed US law tightening export "
                "controls (rules limiting equipment shipments to China) on **ASML** tools. "
                "China still accounts for roughly 20% of ASML's bookings."
            ),
            citations=[Citation(source="Hacker News (ASML query)", url="https://example.com/hn/dutch")],
            category="policy",
        ),
    ]
    digest = Digest(
        date=date(2026, 5, 26),
        email_subject="ASML High-NA ready; Dutch push back on US export rules",
        intro="Two storylines dominate this morning: tool maturity and policy friction.",
        briefs=briefs,
        total_kept=14,
        total_fetched=180,
    )

    templates_dir = Path(__file__).resolve().parents[1] / "src" / "tech_news" / "templates"
    html = render_html(digest, templates_dir)

    # Headlines + paragraphs both render
    assert "High-NA EUV is ready" in html
    assert "Dutch government pushes back" in html
    assert "Netherlands formally objected" in html

    # Markdown bold is promoted to <strong>
    assert "<strong>ASML</strong>" in html

    # Both citations are rendered as links, comma-separated
    assert 'href="https://example.com/semiwiki/asml"' in html
    assert 'href="https://example.com/bitschips/asml"' in html
    assert "SemiWiki" in html
    assert "Bits &amp; Chips" in html or "Bits & Chips" in html

    # Intro renders when present
    assert "Two storylines dominate" in html


def test_template_omits_intro_when_empty():
    digest = Digest(
        date=date(2026, 5, 26),
        email_subject="ASML High-NA ready",
        intro="",
        briefs=[
            Brief(
                headline="x",
                paragraph="y",
                citations=[Citation(source="s", url="https://example.com/")],
                category="tech",
            )
        ],
        total_kept=1,
        total_fetched=1,
    )
    templates_dir = Path(__file__).resolve().parents[1] / "src" / "tech_news" / "templates"
    html = render_html(digest, templates_dir)
    assert 'class="intro"' not in html


def test_bold_md_filter_promotes_markdown_and_escapes_html():
    from tech_news.mailer import _bold_md

    assert _bold_md("**ASML** ships High-NA") == "<strong>ASML</strong> ships High-NA"
    out = _bold_md("**A** and **B**")
    assert "<strong>A</strong>" in out and "<strong>B</strong>" in out
    out = _bold_md("**<script>x</script>** is bad")
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert _bold_md("") == ""


def test_digest_date_helpers_are_cross_platform():
    d = Digest(
        date=date(2026, 1, 5),
        email_subject="x",
        intro="",
        briefs=[],
        total_kept=0,
        total_fetched=0,
    )
    assert d.date_short == "Jan 5, 2026"
    assert "January 5, 2026" in d.date_long
