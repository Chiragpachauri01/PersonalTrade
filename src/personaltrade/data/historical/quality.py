"""Data-quality checks for candle frames.

Errors mean the data violates hard invariants (duplicates, impossible OHLC).
Warnings flag things worth eyes (calendar gaps, price spikes) — spikes can be
legitimate (corporate actions) and gap detection is only as good as the holiday
file, so neither blocks storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from personaltrade.core.calendar import IST, NSECalendar
from personaltrade.core.enums import Interval

Severity = Literal["error", "warning"]

#: |close-to-close return| above this is flagged as a spike
SPIKE_THRESHOLDS: dict[Interval, float] = {
    Interval.D1: 0.25,
    Interval.M15: 0.10,
    Interval.M1: 0.05,
}

_MAX_LISTED = 10  # cap listed examples per finding


@dataclass(frozen=True)
class Finding:
    severity: Severity
    kind: str
    detail: str


@dataclass
class QualityReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    @property
    def status(self) -> str:
        if self.has_errors:
            return "errors"
        return "warnings" if self.findings else "ok"

    def summary(self) -> str:
        if not self.findings:
            return "ok"
        return "; ".join(f"[{f.severity}] {f.kind}: {f.detail}" for f in self.findings)


def check_candles(
    frame: pd.DataFrame,
    interval: Interval,
    calendar: NSECalendar | None = None,
) -> QualityReport:
    report = QualityReport()
    if frame.empty:
        report.findings.append(Finding("error", "empty", "no candles returned"))
        return report

    dup = int(frame["ts"].duplicated().sum())
    if dup:
        report.findings.append(Finding("error", "duplicates", f"{dup} duplicate timestamps"))

    if not frame["ts"].is_monotonic_increasing:
        report.findings.append(Finding("error", "unsorted", "timestamps not ascending"))

    bad_ohlc = frame[
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
    ]
    if not bad_ohlc.empty:
        examples = [str(t) for t in bad_ohlc["ts"].head(_MAX_LISTED)]
        report.findings.append(
            Finding("error", "bad_ohlc", f"{len(bad_ohlc)} rows violate OHLC bounds: {examples}")
        )

    negative_volume = int((frame["volume"] < 0).sum())
    if negative_volume:
        report.findings.append(
            Finding("error", "negative_volume", f"{negative_volume} rows with volume < 0")
        )

    threshold = SPIKE_THRESHOLDS[interval]
    returns = frame["close"].pct_change().abs()
    spikes = frame[returns > threshold]
    if not spikes.empty:
        examples = [str(t) for t in spikes["ts"].head(_MAX_LISTED)]
        report.findings.append(
            Finding(
                "warning",
                "price_spike",
                f"{len(spikes)} close-to-close moves > {threshold:.0%} "
                f"(corporate action?): {examples}",
            )
        )

    if calendar is not None and interval == Interval.D1 and len(frame) > 1:
        have = {ts.tz_convert(IST).date() for ts in frame["ts"]}
        first, last = min(have), max(have)
        expected = calendar.trading_days_between(first, last)
        missing = [d for d in expected if d not in have]
        if missing:
            examples = [d.isoformat() for d in missing[:_MAX_LISTED]]
            report.findings.append(
                Finding(
                    "warning",
                    "missing_days",
                    f"{len(missing)} expected trading days absent "
                    f"(check holiday file too): {examples}",
                )
            )

    return report
