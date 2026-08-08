"""Integration test fixtures.

These run against a real PostgreSQL instance in a container, migrated with the
real Alembic migrations. Substituting SQLite would be faster and would prove
much less: the schema leans on PostgreSQL specifics — a partial index on open
incidents, `make_interval()` in the dispatcher query — that SQLite would
silently fail to exercise. A migration that works here works in production.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Provide a migrated PostgreSQL database for the session.

    Two sources, same result. If TEST_DATABASE_URL is set, that database is
    used as-is — CI supplies one as a service container, which avoids relying
    on Docker-in-Docker on the runner. Otherwise a container is started here,
    so a developer needs nothing but a working Docker socket.

    Either way the real Alembic migrations build the schema. Using
    `metadata.create_all` instead would let a broken migration pass CI.
    """
    external = os.environ.get("TEST_DATABASE_URL")

    if external:
        os.environ["DATABASE_URL"] = external
        _reset_caches()
        _migrate()
        yield external
        _reset_caches()
        return

    with PostgresContainer("postgres:16-alpine", driver=None) as container:
        url = container.get_connection_url()
        os.environ["DATABASE_URL"] = url

        # Settings and engines are lru_cached per process. Clear them so the
        # container URL is picked up rather than the default localhost DSN.
        _reset_caches()
        _migrate()

        yield url

    _reset_caches()


def _migrate() -> None:
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")


def _reset_caches() -> None:
    from app import config, db

    config.get_settings.cache_clear()
    db.get_async_engine.cache_clear()
    db.get_async_sessionmaker.cache_clear()
    db.get_sync_engine.cache_clear()
    db.get_sync_sessionmaker.cache_clear()


@pytest.fixture(autouse=True)
def clean_tables(postgres_url: str) -> Iterator[None]:
    """Truncate between tests.

    TRUNCATE rather than a rolled-back transaction: the API under test manages
    its own commits, and wrapping it in an outer transaction would test a
    transactional context that never exists in production.
    """
    yield

    from sqlalchemy import text

    from app.db import get_sync_engine

    with get_sync_engine().begin() as conn:
        conn.execute(text('TRUNCATE "check", incident, target RESTART IDENTITY CASCADE'))


@pytest.fixture
async def client(postgres_url: str) -> AsyncIterator[object]:
    """httpx client bound to the ASGI app — no socket, no live server.

    The async engine is rebuilt per test on purpose. asyncpg connections are
    bound to the event loop that opened them, and pytest-asyncio gives each
    test a fresh loop — a cached engine would hand the second test connections
    belonging to a loop that has already closed.
    """
    from httpx import ASGITransport, AsyncClient

    from app import db
    from app.main import create_app

    db.get_async_engine.cache_clear()
    db.get_async_sessionmaker.cache_clear()

    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            yield async_client
    finally:
        await db.get_async_engine().dispose()
        db.get_async_engine.cache_clear()
        db.get_async_sessionmaker.cache_clear()
