"""`NewsProvider` — the replaceable seam (CLAUDE.md Rule 7) between however a
news source is fetched (RSS today; a paid API later, ADR-023) and everything
downstream (dedup, tagging, persistence), which only ever sees `RawNewsItem`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RawNewsItem:
    """A news item exactly as fetched — untrusted, unsanitized text."""

    source: str
    url: str
    title: str
    body: str
    published_at: datetime | None


class NewsProvider(Protocol):
    """Multiple registered providers run on a schedule (docs/architecture/03-interfaces.md);
    dedup and tagging are shared pipeline code, not the provider's concern."""

    name: str

    def fetch(self, since: datetime) -> list[RawNewsItem]: ...
