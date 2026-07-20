"""Hand-computed golden cost calculations, using the config defaults.

Every case is derived independently in the test comment — turnover=10000
(price 1000 x qty 10) keeps the arithmetic checkable with a calculator.
Default rates: brokerage 0.03%/cap ₹20, STT delivery 0.1% both legs /
intraday 0.025% sell only, exchange 0.0000297, SEBI 0.000001, stamp duty
delivery 0.015% buy / intraday 0.003% buy, GST 18% on brokerage+exchange+SEBI.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from personaltrade.backtest.costs import calculate_costs
from personaltrade.core.config import CostConfig
from personaltrade.core.enums import Segment, Side

RATES = CostConfig()
PRICE = Decimal("1000")
QTY = 10
# turnover = 10000
# brokerage = min(10000*0.0003, 20) = min(3, 20) = 3
# exchange_charges = 10000*0.0000297 = 0.297
# sebi_charges = 10000*0.000001 = 0.01
# gst = (3 + 0.297 + 0.01) * 0.18 = 3.307 * 0.18 = 0.59526  (same every case)
GST = Decimal("0.59526")
BROKERAGE = Decimal("3")
EXCHANGE = Decimal("0.297")
SEBI = Decimal("0.01")


def test_delivery_buy() -> None:
    # stt = 10000*0.001 = 10; stamp_duty(buy,delivery) = 10000*0.00015 = 1.5
    # total = 3 + 10 + 1.5 + 0.297 + 0.01 + 0.59526 = 15.40226
    # net_amount (buy: cost added) = 10000 + 15.40226 = 10015.40226
    result = calculate_costs(Side.BUY, PRICE, QTY, Segment.DELIVERY, RATES)
    assert result.brokerage == BROKERAGE
    assert result.stt == Decimal("10")
    assert result.stamp_duty == Decimal("1.5")
    assert result.exchange_charges == EXCHANGE
    assert result.sebi_charges == SEBI
    assert result.gst == GST
    assert result.total == Decimal("15.40226")
    assert result.net_amount == Decimal("10015.40226")


def test_delivery_sell() -> None:
    # stt = 10 (both legs); stamp_duty = 0 (sell side)
    # total = 3 + 10 + 0 + 0.297 + 0.01 + 0.59526 = 13.90226
    # net_amount (sell: proceeds reduced) = 10000 - 13.90226 = 9986.09774
    result = calculate_costs(Side.SELL, PRICE, QTY, Segment.DELIVERY, RATES)
    assert result.stt == Decimal("10")
    assert result.stamp_duty == Decimal("0")
    assert result.total == Decimal("13.90226")
    assert result.net_amount == Decimal("9986.09774")


def test_intraday_buy() -> None:
    # stt = 0 (buy leg, intraday); stamp_duty(buy,intraday) = 10000*0.00003 = 0.3
    # total = 3 + 0 + 0.3 + 0.297 + 0.01 + 0.59526 = 4.20226
    # net_amount = 10000 + 4.20226 = 10004.20226
    result = calculate_costs(Side.BUY, PRICE, QTY, Segment.INTRADAY, RATES)
    assert result.stt == Decimal("0")
    assert result.stamp_duty == Decimal("0.3")
    assert result.total == Decimal("4.20226")
    assert result.net_amount == Decimal("10004.20226")


def test_intraday_sell() -> None:
    # stt(intraday, sell) = 10000*0.00025 = 2.5; stamp_duty = 0 (sell side)
    # total = 3 + 2.5 + 0 + 0.297 + 0.01 + 0.59526 = 6.40226
    # net_amount = 10000 - 6.40226 = 9993.59774
    result = calculate_costs(Side.SELL, PRICE, QTY, Segment.INTRADAY, RATES)
    assert result.stt == Decimal("2.5")
    assert result.stamp_duty == Decimal("0")
    assert result.total == Decimal("6.40226")
    assert result.net_amount == Decimal("9993.59774")


def test_brokerage_capped_at_flat_max() -> None:
    # turnover = 500*1000 = 500000; pct*turnover = 150 > cap 20 -> brokerage = 20
    result = calculate_costs(Side.BUY, Decimal("500"), 1000, Segment.DELIVERY, RATES)
    assert result.brokerage == Decimal("20")


def test_gst_applies_only_to_brokerage_exchange_sebi() -> None:
    # Manually recompute GST base and confirm STT/stamp_duty are excluded.
    result = calculate_costs(Side.BUY, PRICE, QTY, Segment.DELIVERY, RATES)
    expected_gst_base = result.brokerage + result.exchange_charges + result.sebi_charges
    assert result.gst == expected_gst_base * RATES.gst_pct
    assert result.gst != (expected_gst_base + result.stt + result.stamp_duty) * RATES.gst_pct


def test_total_equals_sum_of_components() -> None:
    result = calculate_costs(Side.BUY, PRICE, QTY, Segment.DELIVERY, RATES)
    assert result.total == (
        result.brokerage
        + result.stt
        + result.stamp_duty
        + result.exchange_charges
        + result.sebi_charges
        + result.gst
    )


def test_rejects_nonpositive_price_or_qty() -> None:
    with pytest.raises(ValueError, match="price must be"):
        calculate_costs(Side.BUY, Decimal("0"), 10, Segment.DELIVERY, RATES)
    with pytest.raises(ValueError, match="qty must be"):
        calculate_costs(Side.BUY, PRICE, 0, Segment.DELIVERY, RATES)


def test_all_money_fields_are_decimal() -> None:
    result = calculate_costs(Side.BUY, PRICE, QTY, Segment.DELIVERY, RATES)
    for field in (
        result.brokerage,
        result.stt,
        result.stamp_duty,
        result.exchange_charges,
        result.sebi_charges,
        result.gst,
        result.total,
        result.net_amount,
    ):
        assert isinstance(field, Decimal)
