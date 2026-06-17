"""Offline tests for fulltext.enrich() — the fetch is always mocked.

These never touch the network: a MagicMock stands in for httpx.Client, returning
canned HTML/headers, so extraction and the graceful-fallback paths are exercised
deterministically.
"""

from unittest.mock import MagicMock

import httpx

from tech_news import fulltext
from tech_news.fulltext import MAX_BODY_CHARS, _extract, _extract_bs4, enrich

# A realistic-enough article page: an <article> with real prose, plus nav/footer
# chrome that a good extractor drops.
ARTICLE_HTML = """
<html><head><title>ASML ships High-NA</title></head>
<body>
  <nav>Home | News | Contact</nav>
  <header>Site banner</header>
  <article>
    <h1>ASML ships its first High-NA EUV system</h1>
    <p>ASML has shipped the first High-NA extreme ultraviolet lithography
    system to a leading logic customer, the company said on Tuesday. The tool
    is expected to enable smaller feature sizes at advanced nodes and marks a
    milestone for the Dutch equipment maker after years of development.</p>
    <p>Installation and qualification will take several months before the
    system enters volume production, executives noted on the earnings call.</p>
  </article>
  <footer>Copyright 2026. All rights reserved.</footer>
  <script>var tracking = true;</script>
</body></html>
"""


def _html_client(html: str, content_type: str = "text/html; charset=utf-8") -> MagicMock:
    """An httpx.Client stand-in whose get() returns `html` with the given type."""
    resp = MagicMock()
    resp.text = html
    resp.headers = {"content-type": content_type}
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    return client


def test_enrich_populates_body_from_html(article_factory):
    a = article_factory(url="https://example.com/asml-high-na", summary="teaser")
    client = _html_client(ARTICLE_HTML)

    fetched = enrich([a], client=client)

    assert fetched == 1
    assert "first High-NA extreme ultraviolet lithography" in a.body
    # Boilerplate chrome is dropped by the extractor.
    assert "All rights reserved" not in a.body
    assert "var tracking" not in a.body
    # The original teaser is left untouched on the article.
    assert a.summary == "teaser"


def test_enrich_returns_count_of_bodies_read(article_factory):
    good = article_factory(url="https://example.com/good")
    # Second URL 403s, so it contributes no body.
    bad = article_factory(url="https://example.com/bad")

    good_resp = MagicMock()
    good_resp.text = ARTICLE_HTML
    good_resp.headers = {"content-type": "text/html"}
    good_resp.raise_for_status.return_value = None

    bad_resp = MagicMock()
    bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403 Forbidden", request=MagicMock(), response=MagicMock()
    )

    client = MagicMock()
    client.get.side_effect = [good_resp, bad_resp]

    fetched = enrich([good, bad], client=client)

    assert fetched == 1
    assert good.body
    assert bad.body == ""


def test_enrich_uses_package_user_agent_when_owning_client(article_factory, mocker):
    # When enrich() creates its own client, it must send the package USER_AGENT.
    a = article_factory(url="https://example.com/a")

    fake_client = _html_client(ARTICLE_HTML)
    ctor = mocker.patch("tech_news.fulltext.httpx.Client", return_value=fake_client)

    enrich([a])

    _, kwargs = ctor.call_args
    assert kwargs["headers"]["User-Agent"] == fulltext.USER_AGENT
    assert kwargs["follow_redirects"] is True
    # A self-created client is closed when done.
    fake_client.close.assert_called_once()


def test_enrich_http_error_degrades_to_empty_body(article_factory):
    a = article_factory(url="https://example.com/timeout", summary="fallback teaser")

    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.ConnectTimeout("timed out")
    client = MagicMock()
    client.get.return_value = resp

    fetched = enrich([a], client=client)

    assert fetched == 0
    assert a.body == ""
    # The summary is the fallback, untouched.
    assert a.summary == "fallback teaser"


def test_enrich_skips_non_html_content_type(article_factory):
    # A PDF (or image) link must not be parsed; body stays empty.
    a = article_factory(url="https://example.com/filing.pdf")
    client = _html_client("%PDF-1.7 binary junk", content_type="application/pdf")

    fetched = enrich([a], client=client)

    assert fetched == 0
    assert a.body == ""


def test_enrich_empty_extraction_leaves_body_empty(article_factory):
    # A page with no extractable prose (empty body) yields no body.
    a = article_factory(url="https://example.com/empty")
    client = _html_client("<html><head><title>Nothing</title></head><body></body></html>")

    fetched = enrich([a], client=client)

    assert fetched == 0
    assert a.body == ""


def test_enrich_respects_max_articles(article_factory):
    arts = [article_factory(url=f"https://example.com/{i}") for i in range(5)]
    client = _html_client(ARTICLE_HTML)

    enrich(arts, max_articles=2, client=client)

    # Only the first two are fetched; the rest are left with empty bodies.
    assert client.get.call_count == 2
    assert all(a.body for a in arts[:2])
    assert all(a.body == "" for a in arts[2:])


def test_enrich_empty_input_returns_zero():
    assert enrich([]) == 0


def test_enrich_caps_body_length(article_factory):
    # A very long body is truncated to MAX_BODY_CHARS.
    long_para = "word " * 5000  # ~25K chars, well past the cap
    html = f"<html><body><article><p>{long_para}</p></article></body></html>"
    a = article_factory(url="https://example.com/long")
    client = _html_client(html)

    enrich([a], client=client)

    assert 0 < len(a.body) <= MAX_BODY_CHARS


def test_extract_falls_back_to_bs4_when_trafilatura_empty(mocker, article_factory):
    # Force trafilatura to return nothing; the bs4 heuristic must recover prose.
    mocker.patch("tech_news.fulltext._extract_trafilatura", return_value="")

    body = _extract(ARTICLE_HTML, "https://example.com/a")

    assert "first High-NA extreme ultraviolet lithography" in body
    # bs4 fallback also drops script/footer chrome.
    assert "var tracking" not in body


def test_extract_bs4_drops_boilerplate_tags():
    html = (
        "<html><body><nav>navlinks</nav>"
        "<article><p>The real article text lives here.</p></article>"
        "<footer>footer junk</footer>"
        "<script>code()</script></body></html>"
    )
    body = _extract_bs4(html)
    assert "The real article text lives here." in body
    assert "navlinks" not in body
    assert "footer junk" not in body
    assert "code()" not in body


def test_extract_returns_empty_on_no_prose():
    assert _extract("<html><body></body></html>", "https://example.com/x") == ""
