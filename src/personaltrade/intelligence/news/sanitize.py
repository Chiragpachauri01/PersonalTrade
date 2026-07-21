"""Untrusted-text hygiene (ROADMAP M13, docs/architecture/05-ai-data-flow.md's
prompt-injection defense #2): strip markup and clamp length *before* a news
item is ever stored, so nothing downstream — the CLI, a future dashboard, or
M14's prompt builder — has to re-sanitize raw feed HTML.

Tags are stripped with `html.parser.HTMLParser`, not a regex: RSS descriptions
are attacker/publisher-controlled and often contain malformed markup, and a
regex tag-stripper is a well-known bypassable approach against exactly that.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(raw: str) -> str:
    """Plain text content only, whitespace-collapsed. Malformed markup degrades
    to "whatever text HTMLParser could recover," never an exception — this
    runs over attacker-controlled input and must not be a crash vector."""
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    return " ".join(parser.text().split())


def clamp(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip() + "…"
