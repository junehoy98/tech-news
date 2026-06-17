from datetime import date
from pathlib import Path

from tech_news.mailer import render_html
from tech_news.synthesize import Brief, Citation, Digest, LeadBrief


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
        lead_brief=None,
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

    # Category tag renders beside each headline (uppercased via CSS)
    assert '<span class="cat">tech</span>' in html
    assert '<span class="cat">policy</span>' in html


def test_template_renders_lead_brief_prominently_first():
    lead = LeadBrief(
        headline="ASML ships its first High-NA EUV scanner",
        paragraph=(
            "**ASML** shipped its first High-NA EUV (next-gen extreme-ultraviolet) "
            "scanner, lifting numerical aperture from 0.33 to **0.55** — the change "
            "that shrinks the printable half-pitch from 13 nm toward 8 nm at the same "
            "13.5 nm wavelength. The larger pupil collects steeper diffraction orders, "
            "so finer features resolve in a single exposure, but it also halves the "
            "field and demands anamorphic optics and tighter overlay (layer-to-layer "
            "alignment) budgets in the low nanometers."
        ),
        citations=[Citation(source="SemiWiki", url="https://example.com/lead/asml")],
        category="tech",
    )
    regular = Brief(
        headline="KLA updates its overlay metrology suite",
        paragraph="**KLA** refreshed its overlay (layer-to-layer alignment) tools.",
        citations=[Citation(source="KLA", url="https://example.com/kla")],
        category="company",
    )
    digest = Digest(
        date=date(2026, 5, 26),
        email_subject="ASML ships High-NA",
        intro="",
        lead_brief=lead,
        briefs=[regular],
        total_kept=5,
        total_fetched=50,
    )

    templates_dir = Path(__file__).resolve().parents[1] / "src" / "tech_news" / "templates"
    html = render_html(digest, templates_dir)

    # Lead brief renders with the prominent lead class and its deeper paragraph.
    assert 'class="brief lead"' in html
    assert "numerical aperture from 0.33" in html

    # The lead appears before the regular brief in the document.
    assert html.index("first High-NA EUV scanner") < html.index("overlay metrology suite")

    # Footer counts the lead plus the regular brief.
    assert "2 briefs from" in html


def test_template_omits_intro_when_empty():
    digest = Digest(
        date=date(2026, 5, 26),
        email_subject="ASML High-NA ready",
        intro="",
        lead_brief=None,
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
        lead_brief=None,
        briefs=[],
        total_kept=0,
        total_fetched=0,
    )
    assert d.date_short == "Jan 5, 2026"
    assert "January 5, 2026" in d.date_long
