"""`RssNewsProvider` — one generic `NewsProvider` implementation parametrized by
feed URL (ROADMAP M13), rather than a bespoke class per source. Registering a
new source, or dropping a dead one, is a config edit (`news.sources` in
default.yaml), never a code change.

Feeds are parsed with `feedparser`, not `xml.etree.ElementTree`: real Indian
financial-news RSS feeds are not reliably well-formed XML (verified directly —
moneycontrol.com's own feed returned outright unparseable content across
separate live requests during this milestone's build). `feedparser` is the
long-standing standard tool for tolerating exactly that mess; a strict parser
is the wrong choice for feeds this codebase doesn't control.
"""

from __future__ import annotations

from calendar import timegm
from datetime import UTC, datetime

import feedparser
import httpx

from personaltrade.core.errors import PersonalTradeError
from personaltrade.intelligence.news.provider import RawNewsItem

_USER_AGENT = "PersonalTrade/0.1 (personal research bot; +https://github.com)"


class NewsFetchError(PersonalTradeError):
    """A news source failed (transport, HTTP error) — never raised for a feed
    that merely parses as empty/malformed; `feedparser` degrades gracefully,
    so a bad feed yields zero items rather than blocking every other source."""


def _published_at(entry: feedparser.FeedParserDict) -> datetime | None:
    parsed = entry.get("published_parsed")
    if parsed is None:
        return None
    # feedparser normalizes this struct_time to UTC already — `calendar.timegm`
    # (not `time.mktime`, which assumes *local* time) is the correct inverse.
    return datetime.fromtimestamp(timegm(parsed), tz=UTC)


class RssNewsProvider:
    def __init__(
        self,
        name: str,
        url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.name = name
        self.url = url
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": _USER_AGENT})

    def fetch(self, since: datetime) -> list[RawNewsItem]:
        try:
            response = self._client.get(self.url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NewsFetchError(f"{self.name}: fetch failed: {exc}") from exc

        parsed = feedparser.parse(response.content)
        items = []
        for entry in parsed.entries:
            link = entry.get("link")
            if not link:
                continue  # can't dedup or attribute an item with no URL
            published = _published_at(entry)
            if published is not None and published < since:
                continue
            items.append(
                RawNewsItem(
                    source=self.name,
                    url=link,
                    title=entry.get("title", ""),
                    body=entry.get("summary", ""),
                    published_at=published,
                )
            )
        return items
