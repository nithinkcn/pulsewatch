"""Liveness and readiness.

Split deliberately. Liveness answers "is this process alive" and must not
touch dependencies — if it did, a brief database blip would make the
orchestrator kill otherwise healthy containers. Readiness answers "should
traffic be routed here" and therefore does check them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.events import get_async_redis

router = APIRouter(tags=["health"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(session: SessionDep, response: Response) -> dict[str, object]:
    checks: dict[str, bool] = {}

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    client = get_async_redis()
    try:
        await client.ping()
        checks["redis"] = True
    except Exception:
        checks["redis"] = False
    finally:
        await client.aclose()

    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready, "checks": checks}
