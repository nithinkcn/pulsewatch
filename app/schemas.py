"""Pydantic v2 API contracts.

The request models are not thin mirrors of the ORM: they carry the validation
that stops bad monitoring config reaching the database. A target with a 1
second interval and a 30 second timeout would queue faster than it drains, so
that combination is rejected at the edge rather than debugged in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import CheckType, TargetStatus

Name = Annotated[str, Field(min_length=1, max_length=200)]


class TargetCreate(BaseModel):
    name: Name
    address: Annotated[str, Field(min_length=1)]
    check_type: CheckType = CheckType.HTTP
    group: Annotated[str | None, Field(max_length=200)] = None
    expected_status: Annotated[int | None, Field(ge=100, le=599)] = 200
    interval_seconds: Annotated[int, Field(ge=5, le=86_400)] = 60
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 10.0
    failure_threshold: Annotated[int, Field(ge=1, le=100)] = 3
    recovery_threshold: Annotated[int, Field(ge=1, le=100)] = 2
    enabled: bool = True

    @model_validator(mode="after")
    def check_timeout_fits_interval(self) -> Self:
        # A probe that can outlast its own schedule produces overlapping runs
        # and a queue that grows without bound.
        if self.timeout_seconds >= self.interval_seconds:
            raise ValueError("timeout_seconds must be less than interval_seconds")
        return self

    @model_validator(mode="after")
    def check_address_matches_type(self) -> Self:
        if self.check_type is CheckType.HTTP and not self.address.startswith(
            ("http://", "https://")
        ):
            raise ValueError("http targets require an http:// or https:// address")
        if self.check_type is CheckType.TCP:
            host, _, port = self.address.rpartition(":")
            if not host or not port.isdigit():
                raise ValueError("tcp targets require a 'host:port' address")
        return self


class TargetUpdate(BaseModel):
    """Partial update. Every field optional; omitted fields are left alone."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    group: Annotated[str | None, Field(max_length=200)] = None
    expected_status: Annotated[int | None, Field(ge=100, le=599)] = None
    interval_seconds: Annotated[int | None, Field(ge=5, le=86_400)] = None
    timeout_seconds: Annotated[float | None, Field(gt=0, le=120)] = None
    failure_threshold: Annotated[int | None, Field(ge=1, le=100)] = None
    recovery_threshold: Annotated[int | None, Field(ge=1, le=100)] = None
    enabled: bool | None = None


class TargetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    group: str | None
    check_type: CheckType
    address: str
    expected_status: int | None
    interval_seconds: int
    timeout_seconds: float
    failure_threshold: int
    recovery_threshold: int
    enabled: bool
    status: TargetStatus
    consecutive_failures: int
    consecutive_successes: int
    last_checked_at: datetime | None
    created_at: datetime


class CheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_id: uuid.UUID
    checked_at: datetime
    ok: bool
    latency_ms: float | None
    status_code: int | None
    error: str | None


class IncidentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_id: uuid.UUID
    started_at: datetime
    resolved_at: datetime | None
    cause: str | None


class AlertEvent(BaseModel):
    """What goes onto the pub/sub channel and out over the WebSocket."""

    event: Literal["target.down", "target.up"]
    target_id: uuid.UUID
    target_name: str
    group: str | None
    at: datetime
    cause: str | None = None
