from unittest.mock import MagicMock

from tech_news.rank import (
    RANKING_BATCH_SIZE,
    ItemScore,
    RankingResponse,
    _format_articles,
    rank_articles,
)


def test_format_articles_includes_fingerprint_and_metadata(article_factory):
    a = article_factory(
        url="https://example.com/asml-news",
        title="ASML ships first High-NA",
        source_name="ASML Press Releases",
        category="company",
    )
    out = _format_articles([a])
    assert a.fingerprint in out
    assert "ASML ships first High-NA" in out
    assert "ASML Press Releases" in out
    assert "topic_tag" in out  # the new clustering field is documented in the prompt


def test_rank_articles_matches_back_by_fingerprint(article_factory, tmp_path):
    a = article_factory(url="https://example.com/one", title="One")
    b = article_factory(url="https://example.com/two", title="Two")

    fake_response = MagicMock()
    fake_response.parsed_output = RankingResponse(
        items=[
            ItemScore(
                fingerprint=a.fingerprint,
                score=8,
                category="tech",
                topic_tag="ASML High-NA shipment",
            ),
            ItemScore(
                fingerprint=b.fingerprint,
                score=3,
                category="business",
                topic_tag="generic AI news",
            ),
        ]
    )
    fake_response.stop_reason = "end_turn"

    fake_client = MagicMock()
    fake_client.messages.parse.return_value = fake_response

    criteria = tmp_path / "criteria.md"
    criteria.write_text("rubric text", encoding="utf-8")

    ranked = rank_articles([a, b], criteria, client=fake_client)

    # Sorted by score desc
    assert [r.article for r in ranked] == [a, b]
    assert ranked[0].score == 8
    assert ranked[0].topic_tag == "ASML High-NA shipment"

    # The rubric is sent as a cache-controlled system block
    call_kwargs = fake_client.messages.parse.call_args.kwargs
    system = call_kwargs["system"]
    assert system == [
        {"type": "text", "text": "rubric text", "cache_control": {"type": "ephemeral"}}
    ]
    assert call_kwargs["output_format"] is RankingResponse


def test_rank_articles_handles_unknown_fingerprint(article_factory, tmp_path):
    a = article_factory()

    fake_response = MagicMock()
    fake_response.parsed_output = RankingResponse(
        items=[
            ItemScore(fingerprint=a.fingerprint, score=7, category="tech", topic_tag="real"),
            ItemScore(
                fingerprint="ghost1234567890ab",
                score=9,
                category="tech",
                topic_tag="hallucinated",
            ),
        ]
    )

    fake_client = MagicMock()
    fake_client.messages.parse.return_value = fake_response

    criteria = tmp_path / "criteria.md"
    criteria.write_text("rubric", encoding="utf-8")

    ranked = rank_articles([a], criteria, client=fake_client)
    assert len(ranked) == 1
    assert ranked[0].article is a


def test_rank_articles_batches_and_keeps_omitted(article_factory, tmp_path):
    # One more than a single batch -> must split into two parse calls.
    n = RANKING_BATCH_SIZE + 1
    articles = [
        article_factory(url=f"https://example.com/{i}", title=f"Item {i}")
        for i in range(n)
    ]

    # Every batch returns no scores, so each article falls back to score 0 —
    # exercising both the batching split and the omission fallback at once.
    empty = MagicMock()
    empty.parsed_output = RankingResponse(items=[])
    empty.stop_reason = "end_turn"
    empty.usage = None
    fake_client = MagicMock()
    fake_client.messages.parse.return_value = empty

    criteria = tmp_path / "criteria.md"
    criteria.write_text("rubric", encoding="utf-8")

    ranked = rank_articles(articles, criteria, client=fake_client)

    assert fake_client.messages.parse.call_count == 2
    # No article is lost; omitted ones are kept at score 0.
    assert {r.article.fingerprint for r in ranked} == {a.fingerprint for a in articles}
    assert all(r.score == 0 for r in ranked)


def test_rank_articles_survives_failed_batch(article_factory, tmp_path):
    a = article_factory(url="https://example.com/x", title="X")

    failed = MagicMock()
    failed.parsed_output = None  # e.g. truncated/overflowed output
    failed.stop_reason = "max_tokens"
    failed.usage = None
    fake_client = MagicMock()
    fake_client.messages.parse.return_value = failed

    criteria = tmp_path / "criteria.md"
    criteria.write_text("rubric", encoding="utf-8")

    # A failed batch doesn't crash; the article is kept at the default score.
    ranked = rank_articles([a], criteria, client=fake_client)
    assert len(ranked) == 1
    assert ranked[0].article is a
    assert ranked[0].score == 0
