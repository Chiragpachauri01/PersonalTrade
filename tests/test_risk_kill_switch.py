from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from personaltrade.core.enums import RiskEventKind
from personaltrade.data.store.repos import RiskEventRepository
from personaltrade.risk.kill_switch import KillSwitch, KillSwitchNotTripped


class TestInitialState:
    def test_starts_clear(self, db_session: Session) -> None:
        state = KillSwitch(db_session).state()
        assert state.tripped is False
        assert state.reason is None
        assert state.tripped_at is None
        assert state.consecutive_errors == 0

    def test_state_is_a_singleton_row(self, db_session: Session) -> None:
        first = KillSwitch(db_session).state()
        db_session.commit()
        second = KillSwitch(db_session).state()
        assert first.id == second.id == 1


class TestTrip:
    def test_trip_sets_tripped_with_reason(self, db_session: Session) -> None:
        ks = KillSwitch(db_session)
        state = ks.trip("manual halt")
        assert state.tripped is True
        assert state.reason == "manual halt"
        assert state.tripped_at is not None

    def test_trip_logs_a_risk_event(self, db_session: Session) -> None:
        KillSwitch(db_session).trip("manual halt", detail={"source": "test"})
        events = RiskEventRepository(db_session).list_all()
        assert len(events) == 1
        assert events[0].kind == RiskEventKind.KILL_SWITCH
        assert events[0].detail["reason"] == "manual halt"
        assert events[0].detail["source"] == "test"

    def test_trip_is_idempotent_no_duplicate_events(self, db_session: Session) -> None:
        ks = KillSwitch(db_session)
        ks.trip("first reason")
        ks.trip("second reason")  # already tripped -> ignored, not overwritten
        state = ks.state()
        assert state.reason == "first reason"
        assert len(RiskEventRepository(db_session).list_all()) == 1


class TestReset:
    def test_reset_clears_state_and_logs_reset_event(self, db_session: Session) -> None:
        ks = KillSwitch(db_session)
        ks.trip("bad day")
        state = ks.reset("reviewed, safe to resume")
        assert state.tripped is False
        assert state.reason is None
        assert state.tripped_at is None

        events = RiskEventRepository(db_session).list_all()
        assert [e.kind for e in events] == [
            RiskEventKind.KILL_SWITCH,
            RiskEventKind.KILL_SWITCH_RESET,
        ]
        assert events[-1].detail["reason"] == "reviewed, safe to resume"

    def test_reset_without_trip_raises(self, db_session: Session) -> None:
        with pytest.raises(KillSwitchNotTripped):
            KillSwitch(db_session).reset("nothing to reset")

    def test_reset_clears_consecutive_error_count(self, db_session: Session) -> None:
        ks = KillSwitch(db_session)
        ks.record_error(max_consecutive_errors=100)
        ks.record_error(max_consecutive_errors=100)
        ks.trip("manual")
        state = ks.reset("ok")
        assert state.consecutive_errors == 0


class TestCircuitBreaker:
    def test_record_error_increments_without_tripping_below_threshold(
        self, db_session: Session
    ) -> None:
        ks = KillSwitch(db_session)
        state = ks.record_error(max_consecutive_errors=5)
        assert state.consecutive_errors == 1
        assert state.tripped is False

    def test_record_error_auto_trips_at_threshold(self, db_session: Session) -> None:
        ks = KillSwitch(db_session)
        for _ in range(4):
            ks.record_error(max_consecutive_errors=5)
        state = ks.record_error(max_consecutive_errors=5)
        assert state.consecutive_errors == 5
        assert state.tripped is True
        assert "5 consecutive errors" in (state.reason or "")

    def test_record_success_resets_counter(self, db_session: Session) -> None:
        ks = KillSwitch(db_session)
        ks.record_error(max_consecutive_errors=5)
        ks.record_error(max_consecutive_errors=5)
        state = ks.record_success()
        assert state.consecutive_errors == 0
        assert state.tripped is False

    def test_record_error_past_threshold_does_not_double_trip(self, db_session: Session) -> None:
        """Once tripped, further errors keep incrementing but don't re-trigger trip()."""
        ks = KillSwitch(db_session)
        for _ in range(7):
            ks.record_error(max_consecutive_errors=5)
        assert len(RiskEventRepository(db_session).list_all()) == 1
