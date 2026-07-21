"""Ingestion pipeline (ROADMAP M13): fetch from every registered `NewsProvider`,
sanitize, dedup, tag against the instrument universe, and persist — one call
per `pt news sync` invocation.

A provider failure (network error, bad feed) is isolated to that provider —
one dead source must never block ingestion from the others.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from personaltrade.core.config import NewsConfig
from personaltrade.data.store.models import NewsInstrumentTag, NewsItem
from personaltrade.data.store.repos import InstrumentRepository, NewsRepository
from personaltrade.intelligence.news.provider import NewsProvider
from personaltrade.intelligence.news.rss import NewsFetchError
from personaltrade.intelligence.news.sanitize import clamp, strip_html
from personaltrade.intelligence.news.tagging import build_matchers, tag_instruments


@dataclass(frozen=True)
class IngestResult:
    source: str
    fetched: int = 0
    stored: int = 0
    error: str | None = None


def ingest(
    session: Session,
    providers: Sequence[NewsProvider],
    *,
    since: datetime,
    cfg: NewsConfig,
) -> list[IngestResult]:
    matchers = build_matchers(InstrumentRepository(session).list_active())
    news_repo = NewsRepository(session)
    results = []

    for provider in providers:
        try:
            raw_items = provider.fetch(since)
        except NewsFetchError as exc:
            results.append(IngestResult(source=provider.name, error=str(exc)))
            continue

        stored = 0
        for raw in raw_items:
            title = clamp(strip_html(raw.title), cfg.max_title_length)
            body = clamp(strip_html(raw.body), cfg.max_body_length)
            item = news_repo.add_if_new(
                NewsItem(
                    source=raw.source,
                    url=raw.url,
                    title=title,
                    body=body,
                    published_at=raw.published_at,
                )
            )
            if item is None:
                continue
            stored += 1
            for instrument_id in tag_instruments(f"{title} {body}", matchers):
                session.add(NewsInstrumentTag(news_item_id=item.id, instrument_id=instrument_id))

        results.append(IngestResult(source=provider.name, fetched=len(raw_items), stored=stored))

    return results
