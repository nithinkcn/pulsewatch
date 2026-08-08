"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api import health, incidents, targets
from app.config import get_settings
from app.db import get_async_engine
from app.ws import router as ws_router


def configure_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Return pooled connections on shutdown so a rolling deploy does not leave
    # PostgreSQL holding sockets for containers that are already gone.
    await get_async_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="pulsewatch",
        version="0.1.0",
        summary="Scheduled uptime monitoring with transition-based alerting.",
        description=(
            "Monitors HTTP and TCP endpoints on a per-target schedule. Alerts fire on "
            "state transitions, not on individual failed probes — one incident per "
            "outage, not one per poll."
        ),
        lifespan=lifespan,
    )

    api_prefix = "/api/v1"
    app.include_router(health.router)
    app.include_router(targets.router, prefix=api_prefix)
    app.include_router(incidents.router, prefix=api_prefix)
    app.include_router(ws_router)

    return app


app = create_app()
