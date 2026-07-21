from __future__ import annotations

import pytest

from personaltrade.data.providers.reconnect import ReconnectPolicy


class TestDelayFor:
    def test_first_attempt_is_base_delay(self) -> None:
        policy = ReconnectPolicy(base_delay=1.0, max_delay=30.0, factor=2.0)
        assert policy.delay_for(0) == 1.0

    def test_exponential_growth(self) -> None:
        policy = ReconnectPolicy(base_delay=1.0, max_delay=30.0, factor=2.0)
        assert policy.delay_for(1) == 2.0
        assert policy.delay_for(2) == 4.0
        assert policy.delay_for(3) == 8.0

    def test_capped_at_max_delay(self) -> None:
        policy = ReconnectPolicy(base_delay=1.0, max_delay=10.0, factor=2.0)
        assert policy.delay_for(10) == 10.0

    def test_negative_attempt_rejected(self) -> None:
        policy = ReconnectPolicy()
        with pytest.raises(ValueError, match="attempt"):
            policy.delay_for(-1)


class TestConstructionValidation:
    def test_non_positive_base_delay_rejected(self) -> None:
        with pytest.raises(ValueError, match="base_delay"):
            ReconnectPolicy(base_delay=0)

    def test_max_delay_below_base_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_delay"):
            ReconnectPolicy(base_delay=5.0, max_delay=1.0)

    def test_factor_must_exceed_one(self) -> None:
        with pytest.raises(ValueError, match="factor"):
            ReconnectPolicy(factor=1.0)
