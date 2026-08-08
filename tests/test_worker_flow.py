"""End-to-end worker behaviour against a real database.

The evaluator tests prove the state machine in isolation. These prove it is
wired up correctly: that a transition actually writes an incident row, that a
sustained outage does not write a second one, and that the dispatcher only
enqueues targets that are genuinely due.

The probe itself is substituted — the network is not what is under test here,
and `tests/test_probes.py` already covers it against real listeners.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.checks.probes import ProbeResult
from app.db import worker_session
from app.models import Check, CheckType, Incident, Target, TargetStatus
from app.schemas import AlertEvent
from app.workers import tasks

pytestmark = pytest.mark.usefixtures("postgres_url")


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[AlertEvent]:
    """Capture published alerts instead of reaching for Redis."""
    published: list[AlertEvent] = []
    monkeypatch.setattr(tasks, "publish_alert", published.append)
    return published


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture what the dispatcher enqueues, without needing a live broker.

    `run_check.delay` would otherwise open a real Redis connection. Whether
    Celery can reach its broker is not what these tests are about — and
    depending on it means the suite passes or fails based on whether the
    developer happens to have Redis running.
    """
    sent: list[str] = []
    monkeypatch.setattr(tasks.run_check, "delay", lambda target_id: sent.append(target_id))
    return sent


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Control what every probe returns."""
    state = {"ok": True}

    def fake_probe(**_: object) -> ProbeResult:
        ok = bool(state["ok"])
        return ProbeResult(
            ok=ok, latency_ms=1.0, status_code=200 if ok else 503, error=None if ok else "boom"
        )

    monkeypatch.setattr(tasks, "run_probe", fake_probe)
    return state


def make_target(**overrides: object) -> uuid.UUID:
    fields: dict[str, object] = {
        "name": "t",
        "address": "https://example.com",
        "check_type": CheckType.HTTP,
        "interval_seconds": 60,
        "timeout_seconds": 5.0,
        "failure_threshold": 3,
        "recovery_threshold": 2,
    }
    fields.update(overrides)
    with worker_session() as session:
        target = Target(**fields)  # type: ignore[arg-type]
        session.add(target)
        session.flush()
        return target.id


def load(target_id: uuid.UUID) -> Target:
    with worker_session() as session:
        target = session.get(Target, target_id)
        assert target is not None
        session.expunge(target)
        return target


def count(model: type, **filters: object) -> int:
    with worker_session() as session:
        stmt = select(func.count()).select_from(model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(model, key) == value)
        return int(session.scalar(stmt) or 0)


class TestCheckRecording:
    def test_every_probe_writes_a_check_row(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()

        for _ in range(5):
            tasks.run_check(str(target_id))

        assert count(Check, target_id=target_id) == 5

    def test_disabled_target_is_skipped(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target(enabled=False)

        assert tasks.run_check(str(target_id)) == "skipped"
        assert count(Check, target_id=target_id) == 0

    def test_missing_target_does_not_raise(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        assert tasks.run_check(str(uuid.uuid4())) == "skipped"


class TestIncidentLifecycle:
    def test_outage_opens_exactly_one_incident(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()
        probe["ok"] = False

        # Ten consecutive failures, threshold of three.
        for _ in range(10):
            tasks.run_check(str(target_id))

        assert load(target_id).status is TargetStatus.DOWN
        assert count(Incident, target_id=target_id) == 1
        assert [a.event for a in alerts] == ["target.down"]

    def test_recovery_resolves_the_open_incident(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()

        probe["ok"] = False
        for _ in range(3):
            tasks.run_check(str(target_id))
        probe["ok"] = True
        for _ in range(2):
            tasks.run_check(str(target_id))

        assert load(target_id).status is TargetStatus.UP
        with worker_session() as session:
            incident = session.scalars(select(Incident)).one()
            assert incident.resolved_at is not None
            assert incident.is_open is False
        assert [a.event for a in alerts] == ["target.down", "target.up"]

    def test_two_outages_produce_two_incidents(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()

        for phase in (False, True, False, True):
            probe["ok"] = phase
            for _ in range(3):
                tasks.run_check(str(target_id))

        assert count(Incident, target_id=target_id) == 2
        with worker_session() as session:
            unresolved = session.scalars(
                select(Incident).where(Incident.resolved_at.is_(None))
            ).all()
            assert unresolved == []

    def test_flapping_target_never_opens_an_incident(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()

        for i in range(20):
            probe["ok"] = i % 2 == 0
            tasks.run_check(str(target_id))

        assert count(Incident, target_id=target_id) == 0
        assert alerts == []

    def test_incident_records_the_probe_error_as_its_cause(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()
        probe["ok"] = False

        for _ in range(3):
            tasks.run_check(str(target_id))

        with worker_session() as session:
            assert session.scalars(select(Incident)).one().cause == "boom"


class TestDispatcher:
    def test_never_checked_target_is_due(self, probe, alerts, enqueued) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()

        assert tasks.dispatch_due_checks() == 1
        assert enqueued == [str(target_id)]

    def test_recently_checked_target_is_not_due(self, probe, alerts, enqueued) -> None:  # type: ignore[no-untyped-def]
        make_target(interval_seconds=3600)
        tasks.dispatch_due_checks()  # stamps last_checked_at
        enqueued.clear()

        assert tasks.dispatch_due_checks() == 0
        assert enqueued == []

    def test_only_due_targets_are_enqueued(self, probe, alerts, enqueued) -> None:  # type: ignore[no-untyped-def]
        due = make_target(name="due", interval_seconds=60)
        make_target(name="not-due", interval_seconds=3600)
        tasks.dispatch_due_checks()
        enqueued.clear()

        with worker_session() as session:
            target = session.get(Target, due)
            assert target is not None
            target.last_checked_at = datetime.now(UTC) - timedelta(seconds=61)

        assert tasks.dispatch_due_checks() == 1
        assert enqueued == [str(due)]

    def test_target_past_its_own_interval_is_due_again(self, probe, alerts, enqueued) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target(interval_seconds=60)
        tasks.dispatch_due_checks()

        # Reach back in time rather than sleeping: each target is scheduled on
        # its own interval, so this is what "a minute later" means to it.
        with worker_session() as session:
            target = session.get(Target, target_id)
            assert target is not None
            target.last_checked_at = datetime.now(UTC) - timedelta(seconds=61)

        assert tasks.dispatch_due_checks() == 1

    def test_disabled_targets_are_never_dispatched(self, probe, alerts, enqueued) -> None:  # type: ignore[no-untyped-def]
        make_target(enabled=False)

        assert tasks.dispatch_due_checks() == 0
        assert enqueued == []

    def test_dispatch_stamps_before_the_probe_runs(self, probe, alerts, enqueued) -> None:  # type: ignore[no-untyped-def]
        # Guards the queue-growth bug: if last_checked_at were only written
        # after a probe finished, a slow target would be re-enqueued on every
        # tick while its first check was still in flight.
        target_id = make_target(interval_seconds=3600)

        tasks.dispatch_due_checks()

        assert load(target_id).last_checked_at is not None
        assert count(Check, target_id=target_id) == 0


class TestPruning:
    def test_old_checks_are_pruned_and_recent_ones_kept(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()
        now = datetime.now(UTC)

        with worker_session() as session:
            session.add(Check(target_id=target_id, checked_at=now, ok=True))
            session.add(Check(target_id=target_id, checked_at=now - timedelta(days=400), ok=False))

        assert tasks.prune_old_checks() == 1
        assert count(Check, target_id=target_id) == 1

    def test_incidents_are_never_pruned(self, probe, alerts) -> None:  # type: ignore[no-untyped-def]
        target_id = make_target()
        with worker_session() as session:
            session.add(
                Incident(target_id=target_id, started_at=datetime.now(UTC) - timedelta(days=400))
            )

        tasks.prune_old_checks()

        assert count(Incident, target_id=target_id) == 1
