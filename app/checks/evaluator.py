"""State machine deciding when a target actually changes state.

This module is deliberately pure: no database, no network, no clock. Given the
current state and one probe result it returns the next state and what should
happen as a consequence. That makes the interesting behaviour — flap damping,
transition detection — testable without spinning up anything.

The rule that matters:

    An alert fires on a *state transition*, never on an individual failed
    probe.

A camera that is down for six hours produces one incident, not one alert per
poll for six hours. This is the difference between a monitoring system an
operator trusts and one they mute in the first week.

Flap damping falls out of the same mechanism: a target must fail
`failure_threshold` times in a row before it is considered DOWN, and succeed
`recovery_threshold` times in a row before it is considered UP again. A single
dropped packet never wakes anyone up.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.models import TargetStatus


class IncidentAction(enum.StrEnum):
    """What the caller must do to the incident table as a result."""

    NONE = "none"
    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class State:
    """The parts of a target this state machine reads and rewrites."""

    status: TargetStatus
    consecutive_failures: int
    consecutive_successes: int


@dataclass(frozen=True, slots=True)
class Evaluation:
    state: State
    transitioned: bool
    incident_action: IncidentAction


def evaluate(
    state: State,
    *,
    probe_ok: bool,
    failure_threshold: int,
    recovery_threshold: int,
) -> Evaluation:
    """Fold one probe result into a target's state.

    Args:
        state: the target's state before this probe.
        probe_ok: whether the probe succeeded.
        failure_threshold: consecutive failures required to declare DOWN.
        recovery_threshold: consecutive successes required to declare UP.

    Returns:
        The new state, whether the confirmed status changed, and the resulting
        incident action.
    """
    if failure_threshold < 1 or recovery_threshold < 1:
        raise ValueError("thresholds must be >= 1")

    # Streak counters: a result of one kind always resets the other.
    if probe_ok:
        failures = 0
        successes = state.consecutive_successes + 1
    else:
        failures = state.consecutive_failures + 1
        successes = 0

    status = state.status
    action = IncidentAction.NONE

    if not probe_ok and status is not TargetStatus.DOWN and failures >= failure_threshold:
        status = TargetStatus.DOWN
        action = IncidentAction.OPEN

    elif probe_ok and status is not TargetStatus.UP and successes >= recovery_threshold:
        # Only a target that was confirmed DOWN has an incident to close.
        # A target coming out of UNKNOWN (freshly created, or never yet
        # probed) transitions to UP without inventing an outage it never had.
        action = IncidentAction.CLOSE if status is TargetStatus.DOWN else IncidentAction.NONE
        status = TargetStatus.UP

    return Evaluation(
        state=State(
            status=status,
            consecutive_failures=failures,
            consecutive_successes=successes,
        ),
        transitioned=status is not state.status,
        incident_action=action,
    )
