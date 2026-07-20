"""One-off generator for tests/golden/reliance_daily_indicators.csv.

Not part of the test suite (no test_ prefix) — run manually to regenerate
after a deliberate, reviewed change to indicator arithmetic:

    uv run python tests/golden/generate_indicators_golden.py

Reads the RELIANCE daily candles synced locally in M4 (data/candles/, git-
ignored) and freezes both the input candles and every indicator's output
into one committed CSV. test_indicators_golden.py recomputes from the
frozen input columns and compares against the frozen output columns, so the
regression check needs no live data or network access.
"""

from __future__ import annotations

from pathlib import Path

from personaltrade.core.enums import Interval
from personaltrade.data.store.candles import CandleStore
from personaltrade.indicators.batch import (
    atr,
    bollinger,
    ema,
    macd,
    obv,
    rsi,
    sma,
    supertrend,
    vwap,
)

OUTPUT = Path(__file__).parent / "reliance_daily_indicators.csv"


def main() -> None:
    store = CandleStore(Path("data/candles"))
    frame = store.read("RELIANCE", "NSE", Interval.D1)
    if frame.empty:
        raise SystemExit(
            "no local RELIANCE data — run `uv run pt data sync RELIANCE --interval 1d` first"
        )

    frame["sma_20"] = sma(frame["close"], 20)
    frame["ema_20"] = ema(frame["close"], 20)
    frame["rsi_14"] = rsi(frame["close"], 14)
    frame["atr_14"] = atr(frame["high"], frame["low"], frame["close"], 14)

    macd_frame = macd(frame["close"])
    frame["macd"] = macd_frame["macd"]
    frame["macd_signal"] = macd_frame["signal"]
    frame["macd_hist"] = macd_frame["hist"]

    bb = bollinger(frame["close"], 20, 2.0)
    frame["bb_middle"] = bb["middle"]
    frame["bb_upper"] = bb["upper"]
    frame["bb_lower"] = bb["lower"]

    frame["vwap"] = vwap(frame["ts"], frame["high"], frame["low"], frame["close"], frame["volume"])
    frame["obv"] = obv(frame["close"], frame["volume"])

    st = supertrend(frame["high"], frame["low"], frame["close"], 10, 3.0)
    frame["supertrend"] = st["supertrend"]
    frame["supertrend_dir"] = st["direction"]

    frame.to_csv(OUTPUT, index=False, float_format="%.10g")
    print(f"wrote {len(frame)} rows to {OUTPUT}")


if __name__ == "__main__":
    main()
