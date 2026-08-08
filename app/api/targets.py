"""Target CRUD and per-target check history."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Check, Target, TargetStatus
from app.schemas import CheckRead, TargetCreate, TargetRead, TargetUpdate

router = APIRouter(prefix="/targets", tags=["targets"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("", response_model=TargetRead, status_code=status.HTTP_201_CREATED)
async def create_target(payload: TargetCreate, session: SessionDep) -> Target:
    target = Target(**payload.model_dump())
    session.add(target)
    await session.commit()
    await session.refresh(target)
    return target


@router.get("", response_model=list[TargetRead])
async def list_targets(
    session: SessionDep,
    group: Annotated[str | None, Query()] = None,
    target_status: Annotated[TargetStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Target]:
    stmt = select(Target).order_by(Target.name).limit(limit).offset(offset)
    if group is not None:
        stmt = stmt.where(Target.group == group)
    if target_status is not None:
        stmt = stmt.where(Target.status == target_status)
    return list((await session.scalars(stmt)).all())


@router.get("/{target_id}", response_model=TargetRead)
async def get_target(target_id: uuid.UUID, session: SessionDep) -> Target:
    return await _get_or_404(session, target_id)


@router.patch("/{target_id}", response_model=TargetRead)
async def update_target(target_id: uuid.UUID, payload: TargetUpdate, session: SessionDep) -> Target:
    target = await _get_or_404(session, target_id)

    # exclude_unset, not exclude_none: an explicit `null` (clearing a group,
    # say) must be distinguishable from a field the client never sent.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(target, field, value)

    if target.timeout_seconds >= target.interval_seconds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "timeout_seconds must be less than interval_seconds",
        )

    await session.commit()
    await session.refresh(target)
    return target


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(target_id: uuid.UUID, session: SessionDep) -> Response:
    target = await _get_or_404(session, target_id)
    await session.delete(target)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{target_id}/checks", response_model=list[CheckRead])
async def list_checks(
    target_id: uuid.UUID,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[Check]:
    await _get_or_404(session, target_id)
    stmt = (
        select(Check)
        .where(Check.target_id == target_id)
        .order_by(Check.checked_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def _get_or_404(session: AsyncSession, target_id: uuid.UUID) -> Target:
    target = await session.get(Target, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "target not found")
    return target
