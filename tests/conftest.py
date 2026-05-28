from datetime import datetime, timezone

import pytest

from tech_news.sources import Article

UTC = timezone.utc


def make_article(
    url: str = "https://example.com/a",
    title: str = "Sample",
    summary: str = "Sample summary",
    source_name: str = "TestSource",
    category: str = "tech",
    priority: int = 1,
) -> Article:
    return Article(
        url=url,
        title=title,
        summary=summary,
        published=datetime(2026, 5, 26, tzinfo=UTC),
        source_name=source_name,
        category=category,
        priority=priority,
    )


@pytest.fixture
def article_factory():
    return make_article
