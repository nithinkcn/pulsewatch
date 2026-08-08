"""Celery application and the Beat schedule."""

from __future__ import annotations

from celery import Celery
from celery.schedules import schedule

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "pulsewatch",
    broker=settings.celery_broker_url,
    backend=None,  # Nothing reads task return values; storing them is pure cost.
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Hand out one message at a time. Probes have wildly different durations
    # (2ms for a healthy local service, the full timeout for a dead one), so
    # prefetching would let one worker sit on a batch of slow checks while
    # another idles.
    worker_prefetch_multiplier=1,
    # Redis is not an AMQP broker: a lost worker's unacked messages are only
    # redelivered after this window. Keep it above the longest possible probe.
    broker_transport_options={"visibility_timeout": 300},
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    beat_schedule={
        "dispatch-due-checks": {
            "task": "app.workers.tasks.dispatch_due_checks",
            "schedule": schedule(run_every=settings.dispatch_interval_seconds),
        },
        "prune-old-checks": {
            "task": "app.workers.tasks.prune_old_checks",
            "schedule": schedule(run_every=3600),
        },
    },
)
