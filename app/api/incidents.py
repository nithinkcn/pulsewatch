"""Incident history — the queryable record of every outage."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Incident
from app.schemas import IncidentRead

router = APIRouter(prefix="/incidents", tags=["incidents"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=list[IncidentRead])
async def list_incidents(
    session: SessionDep,
    target_id: Annotated[uuid.UUID | None, Query()] = None,
    open_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Incident]:
    """List incidents, newest first.

    `open_only=true` is the alerting query — "what is broken right now" — and
    is backed by a partial index so it stays fast regardless of how much
    resolved history has piled up behind it.
    """
    stmt = select(Incident).order_by(Incident.started_at.desc()).limit(limit).offset(offset)
    if target_id is not None:
        stmt = stmt.where(Incident.target_id == target_id)
    if open_only:
        stmt = stmt.where(Incident.resolved_at.is_(None))
    return list((await session.scalars(stmt)).all())
