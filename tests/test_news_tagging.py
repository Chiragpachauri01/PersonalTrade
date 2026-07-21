"""intelligence/news/tagging.py: matching news text against the instrument
universe by ticker symbol and company name (ROADMAP M13).
"""

from __future__ import annotations

from decimal import Decimal

from personaltrade.data.store.models import Instrument
from personaltrade.intelligence.news.tagging import build_matchers, tag_instruments


def _inst(id_: int, symbol: str, name: str | None) -> Instrument:
    return Instrument(
        id=id_,
        symbol=symbol,
        exchange="NSE",
        instrument_key=f"NSE_EQ|{symbol}",
        name=name,
        tick_size=Decimal("0.05"),
    )


class TestSymbolMatching:
    def test_matches_exact_case_ticker(self) -> None:
        matchers = build_matchers([_inst(1, "RELIANCE", None)])
        assert tag_instruments("RELIANCE shares rally on strong Q1", matchers) == [1]

    def test_symbol_matching_is_case_sensitive(self) -> None:
        """A live-E2E finding (ADR-023): many real NSE tickers are also ordinary
        English words (OIL, ENERGY, DOLLAR, ...) — case-insensitive matching
        tagged almost every generic markets article to one of these. Lowercase
        prose use of the word must NOT match the all-caps ticker."""
        matchers = build_matchers([_inst(1, "ENERGY", None)])
        assert tag_instruments("energy stocks rebound as oil prices ease", matchers) == []
        assert tag_instruments("ENERGY posts record quarterly profit", matchers) == [1]

    def test_no_match_returns_empty(self) -> None:
        matchers = build_matchers([_inst(1, "RELIANCE", None)])
        assert tag_instruments("Nifty ends flat, banks drag", matchers) == []

    def test_symbol_match_respects_word_boundaries(self) -> None:
        # "RIL" (Reliance's common short ticker in some contexts) must not
        # match inside an unrelated word like "TRIAL".
        matchers = build_matchers([_inst(1, "RIL", None)])
        assert tag_instruments("the drug TRIAL results are pending", matchers) == []
        assert tag_instruments("RIL posts record profit", matchers) == [1]


class TestCompanyNameMatching:
    def test_matches_full_company_name(self) -> None:
        matchers = build_matchers([_inst(1, "RELIANCE", "Reliance Industries Ltd")])
        text = "Reliance Industries reported strong quarterly earnings"
        assert tag_instruments(text, matchers) == [1]

    def test_corporate_suffix_is_stripped_before_matching(self) -> None:
        matchers = build_matchers([_inst(1, "TCS", "Tata Consultancy Services Limited")])
        text = "Tata Consultancy Services wins a large deal"
        assert tag_instruments(text, matchers) == [1]

    def test_corporation_is_not_stripped_as_a_generic_suffix(self) -> None:
        """Regression (ADR-023, live-E2E finding): "Birla Corporation Ltd"
        naively stripped down to just "Birla" matched every unrelated Aditya
        Birla Group mention. "Corporation" here is part of the distinguishing
        name, not a generic legal-entity suffix like "Ltd"."""
        matchers = build_matchers([_inst(1, "BIRLACORPN", "Birla Corporation Ltd")])
        assert tag_instruments("Aditya Birla Sun Life AMC posts record profit", matchers) == []
        assert tag_instruments("Birla Corporation posts record profit", matchers) == [1]

    def test_no_name_falls_back_to_symbol_only(self) -> None:
        matchers = build_matchers([_inst(1, "TCS", None)])
        assert tag_instruments("TCS wins a large deal", matchers) == [1]
        assert tag_instruments("Tata Consultancy wins a large deal", matchers) == []


class TestMultipleInstruments:
    def test_tags_every_instrument_mentioned(self) -> None:
        matchers = build_matchers(
            [
                _inst(1, "RELIANCE", "Reliance Industries Ltd"),
                _inst(2, "TCS", "Tata Consultancy Services Ltd"),
                _inst(3, "INFY", "Infosys Ltd"),
            ]
        )
        text = "Reliance Industries and TCS both gained while Infosys slipped"
        assert set(tag_instruments(text, matchers)) == {1, 2, 3}

    def test_only_mentioned_instruments_are_tagged(self) -> None:
        matchers = build_matchers(
            [
                _inst(1, "RELIANCE", "Reliance Industries Ltd"),
                _inst(2, "TCS", "Tata Consultancy Services Ltd"),
            ]
        )
        assert tag_instruments("Reliance Industries posts record profit", matchers) == [1]
