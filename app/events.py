"""Alert fan-out over Redis pub/sub.

Workers publish; API processes subscribe and relay to their own WebSocket
clients. The indirection is what makes the API horizontally scalable: a worker
has no idea which API replica a given operator is connected to, and does not
need to. Without this, alerts would only reach browsers that happened to land
on the same process that ran the check.

Pub/sub is fire-and-forget — a client connected during an outage sees it, one
that connects afterwards does not. That is the right trade here: the durable
record of every outage is the `incident` table, which the dashboard loads on
connect. The socket is for liveness, not for history.
"""

from __future__ import annotations

from functools import lru_cache

import redis
import redis.asyncio as aioredis
import structlog

from app.config import get_settings
from app.schemas import AlertEvent

ALERT_CHANNEL = "pulsewatch:alerts"

log = structlog.get_logger(__name__)


@lru_cache
def get_sync_redis() -> redis.Redis:
    """Publisher connection, used from Celery workers."""
    return redis.Redis.from_url(str(get_settings().redis_url), decode_responses=True)


def get_async_redis() -> aioredis.Redis:
    """Subscriber connection, used from the API process.

    Not cached: a pub/sub subscriber holds its connection for the life of the
    subscription, so each listener needs its own.
    """
    # Annotated rather than returned directly: redis-py's `from_url` is
    # untyped, and returning it bare loses the type at every call site.
    client: aioredis.Redis = aioredis.Redis.from_url(
        str(get_settings().redis_url), decode_responses=True
    )
    return client


def publish_alert(event: AlertEvent) -> None:
    """Publish an alert, never letting a broker problem fail the check.

    A check that has already been written to PostgreSQL is a fact. If Redis is
    unreachable we lose a real-time notification, but the incident is recorded
    and the dashboard will still show it on next load — so this is logged and
    swallowed rather than raised into a task retry.
    """
    try:
        get_sync_redis().publish(ALERT_CHANNEL, event.model_dump_json())
    except redis.RedisError:
        log.warning("alert_publish_failed", target_id=str(event.target_id), event=event.event)
