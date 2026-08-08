"""Tests for the transition state machine.

No database, no network, no sleeping — the interesting behaviour of this
system is a pure function, so its tests run in milliseconds.
"""

from __future__ import annotations

import pytest

from app.checks.evaluator import Evaluation, IncidentAction, State, evaluate
from app.models import TargetStatus


def feed(
    state: State, results: list[bool], *, failure: int = 3, recovery: int = 2
) -> list[Evaluation]:
    """Fold a sequence of probe results through the machine."""
    out: list[Evaluation] = []
    for ok in results:
        evaluation = evaluate(
            state, probe_ok=ok, failure_threshold=failure, recovery_threshold=recovery
        )
        out.append(evaluation)
        state = evaluation.state
    return out


UNKNOWN = State(TargetStatus.UNKNOWN, 0, 0)
UP = State(TargetStatus.UP, 0, 5)
DOWN = State(TargetStatus.DOWN, 5, 0)


class TestThresholds:
    def test_single_failure_does_not_take_a_target_down(self) -> None:
        result = evaluate(UP, probe_ok=False, failure_threshold=3, recovery_threshold=2)

        assert result.state.status is TargetStatus.UP
        assert result.state.consecutive_failures == 1
        assert result.transitioned is False
        assert result.incident_action is IncidentAction.NONE

    def test_goes_down_exactly_on_the_threshold(self) -> None:
        results = feed(UP, [False, False, False], failure=3)

        assert [r.state.status for r in results] == [
            TargetStatus.UP,
            TargetStatus.UP,
            TargetStatus.DOWN,
        ]
        assert results[-1].transitioned is True
        assert results[-1].incident_action is IncidentAction.OPEN

    def test_recovers_exactly_on_the_recovery_threshold(self) -> None:
        results = feed(DOWN, [True, True], recovery=2)

        assert results[0].state.status is TargetStatus.DOWN
        assert results[1].state.status is TargetStatus.UP
        assert results[1].incident_action is IncidentAction.CLOSE

    def test_threshold_of_one_reacts_immediately(self) -> None:
        result = evaluate(UP, probe_ok=False, failure_threshold=1, recovery_threshold=1)

        assert result.state.status is TargetStatus.DOWN
        assert result.incident_action is IncidentAction.OPEN

    @pytest.mark.parametrize(("failure", "recovery"), [(0, 1), (1, 0), (-1, 2)])
    def test_thresholds_below_one_are_rejected(self, failure: int, recovery: int) -> None:
        with pytest.raises(ValueError, match="thresholds must be >= 1"):
            evaluate(UP, probe_ok=False, failure_threshold=failure, recovery_threshold=recovery)


class TestAlertOnTransitionOnly:
    """The property the whole design exists to guarantee."""

    def test_sustained_outage_opens_exactly_one_incident(self) -> None:
        # Twenty consecutive failures is one outage, not twenty alerts.
        results = feed(UP, [False] * 20, failure=3)

        opens = [r for r in results if r.incident_action is IncidentAction.OPEN]
        assert len(opens) == 1

    def test_sustained_recovery_closes_exactly_one_incident(self) -> None:
        results = feed(DOWN, [True] * 20, recovery=2)

        closes = [r for r in results if r.incident_action is IncidentAction.CLOSE]
        assert len(closes) == 1

    def test_steady_healthy_target_never_alerts(self) -> None:
        results = feed(UP, [True] * 50)

        assert all(r.incident_action is IncidentAction.NONE for r in results)
        assert all(r.transitioned is False for r in results)

    def test_full_outage_cycle_produces_one_open_and_one_close(self) -> None:
        results = feed(UP, [False] * 5 + [True] * 5, failure=3, recovery=2)

        actions = [
            r.incident_action for r in results if r.incident_action is not IncidentAction.NONE
        ]
        assert actions == [IncidentAction.OPEN, IncidentAction.CLOSE]


class TestFlapDamping:
    def test_alternating_results_never_trip_a_transition(self) -> None:
        # A target flapping up/down/up/down never accumulates a streak, so it
        # never alerts. This is the case that floods an ops team at 3am.
        results = feed(UP, [False, True] * 25, failure=3, recovery=2)

        assert all(r.incident_action is IncidentAction.NONE for r in results)

    def test_one_success_resets_the_failure_streak(self) -> None:
        results = feed(UP, [False, False, True, False, False], failure=3)

        assert results[2].state.consecutive_failures == 0
        assert results[-1].state.consecutive_failures == 2
        assert results[-1].state.status is TargetStatus.UP

    def test_one_failure_resets_the_success_streak(self) -> None:
        results = feed(DOWN, [True, False, True], recovery=2)

        assert results[1].state.consecutive_successes == 0
        assert results[-1].state.consecutive_successes == 1
        assert results[-1].state.status is TargetStatus.DOWN


class TestUnknownState:
    """A freshly created target has never been probed."""

    def test_new_target_becoming_healthy_does_not_close_a_phantom_incident(self) -> None:
        results = feed(UNKNOWN, [True, True], recovery=2)

        assert results[-1].state.status is TargetStatus.UP
        assert results[-1].transitioned is True
        # Nothing was ever open, so there is nothing to close.
        assert results[-1].incident_action is IncidentAction.NONE

    def test_new_target_that_is_dead_from_the_start_opens_an_incident(self) -> None:
        results = feed(UNKNOWN, [False, False, False], failure=3)

        assert results[-1].state.status is TargetStatus.DOWN
        assert results[-1].incident_action is IncidentAction.OPEN


class TestPurity:
    def test_input_state_is_never_mutated(self) -> None:
        before = State(TargetStatus.UP, 2, 0)

        evaluate(before, probe_ok=False, failure_threshold=3, recovery_threshold=2)

        assert before == State(TargetStatus.UP, 2, 0)

    def test_same_input_gives_same_output(self) -> None:
        args = {"probe_ok": False, "failure_threshold": 3, "recovery_threshold": 2}

        assert evaluate(UP, **args) == evaluate(UP, **args)  # type: ignore[arg-type]
