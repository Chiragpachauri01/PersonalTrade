"""Shared test-data builders."""

from __future__ import annotations

import pandas as pd

from personaltrade.data.providers.base import normalize_candle_frame

#: Real RELIANCE daily candles from the Upstox v3 API (captured 2026-07-19),
#: as returned on the wire: newest-first, IST offsets, [ts, o, h, l, c, vol, oi].
RELIANCE_DAILY_CANDLES: list[list[object]] = [
    ["2026-07-17T00:00:00+05:30", 1300.0, 1330.3, 1296.1, 1327.2, 18302218, 0],
    ["2026-07-16T00:00:00+05:30", 1310.1, 1315.0, 1296.4, 1299.6, 12645128, 0],
    ["2026-07-15T00:00:00+05:30", 1294.1, 1312.6, 1294.1, 1309.5, 11456209, 0],
    ["2026-07-14T00:00:00+05:30", 1299.0, 1305.0, 1291.3, 1295.7, 9834511, 0],
    ["2026-07-13T00:00:00+05:30", 1305.4, 1311.5, 1297.0, 1299.9, 8672233, 0],
    ["2026-07-10T00:00:00+05:30", 1312.0, 1318.9, 1302.5, 1306.2, 10233417, 0],
    ["2026-07-09T00:00:00+05:30", 1308.7, 1316.4, 1305.1, 1312.9, 9128840, 0],
    ["2026-07-08T00:00:00+05:30", 1301.2, 1312.0, 1298.8, 1308.4, 8556120, 0],
    ["2026-07-07T00:00:00+05:30", 1296.5, 1305.9, 1293.2, 1300.8, 7998454, 0],
    ["2026-07-06T00:00:00+05:30", 1303.8, 1308.2, 1294.7, 1297.3, 8110236, 0],
    ["2026-07-03T00:00:00+05:30", 1310.2, 1314.6, 1301.9, 1305.5, 7684521, 0],
    ["2026-07-02T00:00:00+05:30", 1305.0, 1316.8, 1303.4, 1311.7, 8291374, 0],
    ["2026-07-01T00:00:00+05:30", 1298.9, 1312.2, 1296.5, 1308.0, 7001401, 0],
]


def wire_candles_payload(candles: list[list[object]]) -> dict[str, object]:
    """The exact envelope the Upstox v3 historical endpoint returns."""
    return {"status": "success", "data": {"candles": candles}}


def daily_frame(candles: list[list[object]] | None = None) -> pd.DataFrame:
    """A normalized candle frame (UTC, ascending) from wire-format rows."""
    rows = candles if candles is not None else RELIANCE_DAILY_CANDLES
    frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "oi"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return normalize_candle_frame(frame)
