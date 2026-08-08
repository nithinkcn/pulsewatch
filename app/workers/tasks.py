"""Celery tasks.

The scheduling design is a two-stage dispatch, and it is worth explaining
because the obvious alternative does not survive contact with real workloads.

The obvious approach is one Beat entry per target. That fails as soon as
targets are user-managed: Beat's schedule is process state, so every create,
edit and delete needs a Beat restart, and a thousand targets means a thousand
schedule entries evaluated on every tick.

Instead Beat runs a single cheap task on a fixed tick. That task queries which
targets are *due* — based on each target's own interval and when it was last
checked — and enqueues one job per due target. Targets become ordinary rows.
Adding one takes effect on the next tick with nothing restarted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.checks.evaluator import IncidentAction, State, evaluate
from app.checks.probes import run_probe
from app.config import get_settings
from app.db import worker_session
from app.events import publish_alert
from app.models import Check, Incident, Target
from app.schemas import AlertEvent
from app.workers.celery_app import celery_app

log = structlog.get_logger(__name__)


@celery_app.task(name="app.workers.tasks.dispatch_due_checks")
def dispatch_due_checks() -> int:
    """Find targets whose next check is due and enqueue one job each.

    Returns the number of checks enqueued (handy in logs and tests).
    """
    now = datetime.now(UTC)
    with worker_session() as session:
        # "Due" means never checked, or checked longer ago than its own
        # interval. make_interval() builds the comparison per row, so each
        # target is scheduled on its own cadence in a single query rather
        # than being filtered in Python.
        next_due_at = Target.last_checked_at + func.make_interval(
            0, 0, 0, 0, 0, 0, Target.interval_seconds
        )
        due = session.scalars(
            select(Target.id).where(
                Target.enabled.is_(True),
                Target.last_checked_at.is_(None) | (next_due_at <= now),
            )
        ).all()

        # Stamp last_checked_at at dispatch time, not when the probe finishes.
        # Otherwise a target whose probe takes longer than its interval gets
        # re-enqueued on every tick while the first check is still running,
        # and the queue grows without bound.
        if due:
            session.execute(update(Target).where(Target.id.in_(due)).values(last_checked_at=now))

    for target_id in due:
        run_check.delay(str(target_id))

    if due:
        log.info("checks_dispatched", count=len(due))
    return len(due)


@celery_app.task(
    name="app.workers.tasks.run_check",
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def run_check(target_id: str) -> str:
    """Probe one target, record the result, and act on any state change.

    Note what is *not* retried: a failed probe. A target being unreachable is
    the signal this system exists to capture, not an error to paper over. Only
    infrastructure faults on our side reach the retry policy.
    """
    settings = get_settings()

    with worker_session() as session:
        target = session.get(Target, uuid.UUID(target_id))
        if target is None or not target.enabled:
            return "skipped"

        result = run_probe(
            check_type=target.check_type,
            address=target.address,
            timeout=min(target.timeout_seconds, settings.max_probe_timeout_seconds),
            expected_status=target.expected_status,
        )

        now = datetime.now(UTC)
        session.add(
            Check(
                target_id=target.id,
                checked_at=now,
                ok=result.ok,
                latency_ms=result.latency_ms,
                status_code=result.status_code,
                error=result.error,
            )
        )

        outcome = evaluate(
            State(
                status=target.status,
                consecutive_failures=target.consecutive_failures,
                consecutive_successes=target.consecutive_successes,
            ),
            probe_ok=result.ok,
            failure_threshold=target.failure_threshold,
            recovery_threshold=target.recovery_threshold,
        )

        target.status = outcome.state.status
        target.consecutive_failures = outcome.state.consecutive_failures
        target.consecutive_successes = outcome.state.consecutive_successes
        target.last_checked_at = now

        alert = _apply_incident_action(
            session,
            target=target,
            action=outcome.incident_action,
            now=now,
            cause=result.error,
        )

    # Published after the transaction commits. Publishing inside would risk
    # announcing an outage that then rolled back.
    if alert is not None:
        publish_alert(alert)

    return outcome.state.status.value


def _apply_incident_action(
    session: Session,
    *,
    target: Target,
    action: IncidentAction,
    now: datetime,
    cause: str | None,
) -> AlertEvent | None:
    """Open or close an incident, returning the alert to publish (if any)."""
    if action is IncidentAction.NONE:
        return None

    if action is IncidentAction.OPEN:
        session.add(Incident(target_id=target.id, started_at=now, cause=cause))
        log.warning("target_down", target=target.name, cause=cause)
        return AlertEvent(
            event="target.down",
            target_id=target.id,
            target_name=target.name,
            group=target.group,
            at=now,
            cause=cause,
        )

    # CLOSE — resolve whichever incident is still open for this target.
    open_incident = session.scalars(
        select(Incident)
        .where(Incident.target_id == target.id, Incident.resolved_at.is_(None))
        .order_by(Incident.started_at.desc())
        .limit(1)
    ).first()
    if open_incident is not None:
        open_incident.resolved_at = now

    log.info("target_recovered", target=target.name)
    return AlertEvent(
        event="target.up",
        target_id=target.id,
        target_name=target.name,
        group=target.group,
        at=now,
    )


@celery_app.task(name="app.workers.tasks.prune_old_checks")
def prune_old_checks() -> int:
    """Drop probe history past the retention window.

    `check` is the only table that grows without bound — one row per target
    per interval, forever. Incidents are kept indefinitely; they are small and
    they are the part with long-term value.
    """
    cutoff = datetime.now(UTC) - timedelta(days=get_settings().check_retention_days)
    with worker_session() as session:
        # Session.execute is typed as returning Result; DML actually returns a
        # CursorResult, which is the only one carrying rowcount.
        result = cast(
            "CursorResult[Any]", session.execute(delete(Check).where(Check.checked_at < cutoff))
        )
        deleted = result.rowcount

    if deleted:
        log.info("checks_pruned", count=deleted, cutoff=cutoff.isoformat())
    return int(deleted)
