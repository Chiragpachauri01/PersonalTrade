"""Instrument tagging (ROADMAP M13): which instruments does a news item concern?

Matches on both ticker symbol and company name — symbol-only matching misses
most prose, which rarely spells out a raw NSE ticker ("Reliance Industries"
never says "RELIANCE"). `Instrument.name` is Upstox's own instrument-master
company name, fetched since M4 but only persisted starting this milestone.

Symbol matching is case-SENSITIVE; company-name matching is not. This was a
live-E2E finding, not an upfront design choice: a large slice of the ~2,500
NSE tickers are also ordinary English words (OIL, ENERGY, DOLLAR, TOTAL, IT,
GLOBAL, TECH, METAL, RETAIL, MIDCAP, ...), and case-insensitive matching
tagged nearly every generic markets article to whichever of these happened to
appear in a sentence ("Dollar wavers as markets grapple...", "chip stocks
rebound" -> tagged ENERGY). Real ticker mentions are conventionally written
in the exact ticker case; ordinary prose using the same word essentially
never is. This isn't airtight for very short symbols that double as industry
acronyms (e.g. "IT" for "Information Technology" sector coverage, itself
written in caps) — a known, accepted residual ambiguity for a v1, not
something chased further here (see ADR-023).

Matchers are precompiled once per ingestion run (`build_matchers`), not once
per news item — the instrument universe is ~2,500 rows, and recompiling a
regex per row per item would be pointlessly wasteful for a batch job.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from personaltrade.data.store.models import Instrument

#: Pure legal-entity-type suffixes, stripped so "Reliance Industries Ltd" and
#: "Reliance Industries" both match a mention of "Reliance Industries".
#: Deliberately excludes "corp"/"corporation": unlike "Ltd"/"Limited"/"Inc"/
#: "Plc", it is sometimes the actual distinguishing second word of an Indian
#: company's name (e.g. "Birla Corporation") rather than a generic legal
#: suffix — stripping it there left just "Birla", which then matched every
#: unrelated Aditya Birla Group mention (a real live-E2E finding, ADR-023).
_SUFFIX_RE = re.compile(r"\b(ltd|limited|inc|incorporated|plc)\.?\b", re.IGNORECASE)


def _normalize_name(name: str) -> str:
    return " ".join(_SUFFIX_RE.sub("", name).split())


@dataclass(frozen=True)
class _Matcher:
    instrument_id: int
    symbol_pattern: re.Pattern[str]
    name_pattern: re.Pattern[str] | None


def build_matchers(instruments: Sequence[Instrument]) -> list[_Matcher]:
    matchers = []
    for inst in instruments:
        symbol_pattern = re.compile(
            rf"\b{re.escape(inst.symbol)}\b"
        )  # case-sensitive; see module docstring
        name_pattern = None
        if inst.name:
            normalized = _normalize_name(inst.name)
            if normalized:
                name_pattern = re.compile(rf"\b{re.escape(normalized)}\b", re.IGNORECASE)
        matchers.append(_Matcher(inst.id, symbol_pattern, name_pattern))
    return matchers


def tag_instruments(text: str, matchers: Sequence[_Matcher]) -> list[int]:
    """Instrument IDs whose symbol or company name appears in `text` (title +
    sanitized body, typically). Order follows `matchers`, not relevance."""
    matched = []
    for matcher in matchers:
        if matcher.symbol_pattern.search(text) or (
            matcher.name_pattern is not None and matcher.name_pattern.search(text)
        ):
            matched.append(matcher.instrument_id)
    return matched
