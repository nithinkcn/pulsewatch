"""Database engines.

Two of them, on purpose:

  async (asyncpg)  — the FastAPI request path, which is IO-bound and benefits
                     from not blocking the loop.
  sync  (psycopg)  — Celery workers and Alembic, which are plain synchronous
                     processes. Driving an async engine from a Celery task
                     means creating and tearing down an event loop per task,
                     and asyncpg connections do not survive their loop.

Both read the same `DATABASE_URL` and map the same models, so there is one
schema and one source of truth — only the driver differs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


# --- async (API) --------------------------------------------------------
@lru_cache
def get_async_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.async_database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
    )


@lru_cache
def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with get_async_sessionmaker()() as session:
        yield session


# --- sync (workers, migrations) -----------------------------------------
@lru_cache
def get_sync_engine():  # type: ignore[no-untyped-def]
    settings = get_settings()
    return create_engine(settings.sync_database_url, pool_pre_ping=True, pool_size=5)


@lru_cache
def get_sync_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)


@contextmanager
def worker_session() -> Iterator[Session]:
    """Transactional session for a Celery task.

    Commits on success, rolls back on any exception. A task that raises must
    never leave a half-written check row behind.
    """
    session = get_sync_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
