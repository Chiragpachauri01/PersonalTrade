"""intelligence/news/pipeline.py: fetch -> sanitize -> dedup -> tag -> persist,
and provider-failure isolation (ROADMAP M13).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from personaltrade.core.config import NewsConfig
from personaltrade.data.store.models import Instrument
from personaltrade.data.store.repos import InstrumentRepository, NewsRepository
from personaltrade.intelligence.news.pipeline import IngestResult, ingest
from personaltrade.intelligence.news.provider import RawNewsItem
from personaltrade.intelligence.news.rss import NewsFetchError


class FakeProvider:
    """NewsProvider test double: returns a fixed, scripted list of items."""

    def __init__(self, name: str, items: list[RawNewsItem]) -> None:
        self.name = name
        self._items = items

    def fetch(self, since: datetime) -> list[RawNewsItem]:
        return [i for i in self._items if i.published_at is None or i.published_at >= since]


class FailingProvider:
    def __init__(self, name: str, message: str = "boom") -> None:
        self.name = name
        self._message = message

    def fetch(self, since: datetime) -> list[RawNewsItem]:
        raise NewsFetchError(self._message)


@pytest.fixture()
def reliance(db_session: Session) -> Instrument:
    inst = InstrumentRepository(db_session).add(
        Instrument(
            symbol="RELIANCE",
            exchange="NSE",
            instrument_key="NSE_EQ|RELIANCE",
            name="Reliance Industries Ltd",
            tick_size=Decimal("0.05"),
        )
    )
    db_session.flush()
    return inst


_SINCE = datetime(2020, 1, 1, tzinfo=UTC)


def _item(url: str, title: str, body: str = "") -> RawNewsItem:
    return RawNewsItem(
        source="test", url=url, title=title, body=body, published_at=datetime.now(UTC)
    )


class TestIngest:
    def test_stores_new_items_and_tags_matching_instruments(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        provider = FakeProvider(
            "test_source", [_item("https://x/1", "Reliance Industries posts record profit")]
        )
        results = ingest(db_session, [provider], since=_SINCE, cfg=NewsConfig())

        assert results == [IngestResult(source="test_source", fetched=1, stored=1, error=None)]
        news_items = NewsRepository(db_session).list_for_instrument(reliance.id, _SINCE)
        assert len(news_items) == 1
        assert news_items[0].title == "Reliance Industries posts record profit"

    def test_dedups_by_url_across_two_ingest_runs(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        provider = FakeProvider("test_source", [_item("https://x/1", "Reliance Industries gains")])

        first = ingest(db_session, [provider], since=_SINCE, cfg=NewsConfig())
        second = ingest(db_session, [provider], since=_SINCE, cfg=NewsConfig())

        assert first[0].stored == 1
        assert second[0].stored == 0  # same URL, already stored
        assert len(NewsRepository(db_session).list_for_instrument(reliance.id, _SINCE)) == 1

    def test_html_is_stripped_before_storage(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        provider = FakeProvider(
            "test_source",
            [_item("https://x/1", "<b>Reliance Industries</b> posts record profit")],
        )
        ingest(db_session, [provider], since=_SINCE, cfg=NewsConfig())

        [item] = NewsRepository(db_session).list_for_instrument(reliance.id, _SINCE)
        assert "<b>" not in item.title
        assert item.title == "Reliance Industries posts record profit"

    def test_unrelated_item_is_stored_but_not_tagged(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        provider = FakeProvider("test_source", [_item("https://x/1", "Nifty ends flat")])
        results = ingest(db_session, [provider], since=_SINCE, cfg=NewsConfig())

        assert results[0].stored == 1
        assert NewsRepository(db_session).list_for_instrument(reliance.id, _SINCE) == []

    def test_one_failing_provider_does_not_block_others(
        self, db_session: Session, reliance: Instrument
    ) -> None:
        good = FakeProvider("good_source", [_item("https://x/1", "Reliance Industries rallies")])
        bad = FailingProvider("bad_source", "feed unreachable")

        results = ingest(db_session, [bad, good], since=_SINCE, cfg=NewsConfig())
        by_source = {r.source: r for r in results}

        assert by_source["bad_source"].error == "feed unreachable"
        assert by_source["bad_source"].stored == 0
        assert by_source["good_source"].stored == 1
        assert by_source["good_source"].error is None
