"""intelligence/news/rss.py: RssNewsProvider over a mocked HTTP transport — no
real network calls (ROADMAP M13; the real feeds are exercised only by this
milestone's live E2E verification, same precedent as data/historical/sync.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from personaltrade.intelligence.news.rss import NewsFetchError, RssNewsProvider

_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Test Feed</title>
<item>
  <title>Reliance posts record profit</title>
  <link>https://example.com/reliance-profit</link>
  <description>&lt;p&gt;Reliance Industries beat estimates&lt;/p&gt;</description>
  <pubDate>Tue, 21 Jul 2026 10:00:00 +0000</pubDate>
</item>
<item>
  <title>Old story from last year</title>
  <link>https://example.com/old-story</link>
  <description>Stale news</description>
  <pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate>
</item>
<item>
  <title>No link item</title>
  <description>Cannot be deduped or attributed without a URL</description>
  <pubDate>Tue, 21 Jul 2026 10:00:00 +0000</pubDate>
</item>
</channel></rss>
"""

_MALFORMED_FEED = '<rss version="2.0"><channel><item><title>Broken'


def _provider(
    handler: httpx.MockTransport, url: str = "https://example.com/feed.xml"
) -> RssNewsProvider:
    client = httpx.Client(transport=handler)
    return RssNewsProvider("test_source", url, client=client)


class TestFetch:
    def test_parses_items_with_link_title_body_and_published_at(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=_FEED))
        provider = _provider(transport)

        items = provider.fetch(since=datetime(2020, 1, 1, tzinfo=UTC))
        by_url = {i.url: i for i in items}

        assert "https://example.com/reliance-profit" in by_url
        item = by_url["https://example.com/reliance-profit"]
        assert item.source == "test_source"
        assert item.title == "Reliance posts record profit"
        assert "<p>" in item.body  # raw, unsanitized — sanitize.py's job, not the provider's
        assert item.published_at == datetime(2026, 7, 21, 10, 0, 0, tzinfo=UTC)

    def test_items_without_a_link_are_skipped(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=_FEED))
        provider = _provider(transport)

        items = provider.fetch(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert all(i.title != "No link item" for i in items)

    def test_since_filters_out_older_items(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=_FEED))
        provider = _provider(transport)

        items = provider.fetch(since=datetime(2026, 1, 1, tzinfo=UTC))
        assert all(i.url != "https://example.com/old-story" for i in items)

    def test_malformed_feed_degrades_to_empty_list_not_exception(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=_MALFORMED_FEED))
        provider = _provider(transport)

        items = provider.fetch(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert items == []

    def test_http_error_raises_news_fetch_error(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(503))
        provider = _provider(transport)

        with pytest.raises(NewsFetchError):
            provider.fetch(since=datetime(2020, 1, 1, tzinfo=UTC))
