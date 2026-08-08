"""SQLAlchemy 2.0 models.

Three tables, and the split between them is the core design decision:

  target    — configuration plus *current* state. Small, hot, always queried.
  check     — append-only probe results. Large, time-series, rarely queried
              outside a single target's recent history.
  incident  — the periods a target spent down. This is what humans and
              alerting actually care about.

Keeping `check` append-only means the dashboard never scans it to answer
"what is broken right now" — that question is answered by `target.status`
and open rows in `incident`, both of which stay small.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TargetStatus(enum.StrEnum):
    """Confirmed state of a target — only changes when a threshold is met."""

    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


class CheckType(enum.StrEnum):
    HTTP = "http"
    TCP = "tcp"


def _uuid_col() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Target(Base):
    __tablename__ = "target"

    id: Mapped[uuid.UUID] = _uuid_col()
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Free-text grouping (a site, a hospital, a region). Deliberately not a
    # foreign key — grouping schemes change more often than monitoring does.
    group: Mapped[str | None] = mapped_column(String(200), index=True)

    check_type: Mapped[CheckType] = mapped_column(
        Enum(CheckType, name="check_type", native_enum=False, length=16),
        nullable=False,
        default=CheckType.HTTP,
    )
    # HTTP: full URL. TCP: "host:port".
    address: Mapped[str] = mapped_column(Text, nullable=False)
    expected_status: Mapped[int | None] = mapped_column(Integer, default=200)

    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)

    failure_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    recovery_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)

    # --- live state, maintained by the worker ---------------------------
    status: Mapped[TargetStatus] = mapped_column(
        Enum(TargetStatus, name="target_status", native_enum=False, length=16),
        nullable=False,
        default=TargetStatus.UNKNOWN,
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    checks: Mapped[list[Check]] = relationship(
        back_populates="target", cascade="all, delete-orphan", passive_deletes=True
    )
    incidents: Mapped[list[Incident]] = relationship(
        back_populates="target", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("interval_seconds >= 5", name="ck_target_interval_min"),
        CheckConstraint("timeout_seconds > 0", name="ck_target_timeout_positive"),
        CheckConstraint("failure_threshold >= 1", name="ck_target_failure_threshold"),
        CheckConstraint("recovery_threshold >= 1", name="ck_target_recovery_threshold"),
        # The dispatcher's hot query: enabled targets ordered by staleness.
        Index("ix_target_due", "enabled", "last_checked_at"),
    )


class Check(Base):
    """One probe result. Append-only — nothing ever updates a row here."""

    __tablename__ = "check"

    id: Mapped[uuid.UUID] = _uuid_col()
    target_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("target.id", ondelete="CASCADE"), nullable=False
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ok: Mapped[bool] = mapped_column(nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    target: Mapped[Target] = relationship(back_populates="checks")

    __table_args__ = (
        # Every read of this table is "recent checks for one target".
        Index("ix_check_target_time", "target_id", "checked_at"),
    )


class Incident(Base):
    """A period during which a target was confirmed down.

    Opened on an up→down transition, closed on down→up. One row per outage,
    not one per failed probe — that distinction is the whole point.
    """

    __tablename__ = "incident"

    id: Mapped[uuid.UUID] = _uuid_col()
    target_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("target.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cause: Mapped[str | None] = mapped_column(Text)

    target: Mapped[Target] = relationship(back_populates="incidents")

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    __table_args__ = (
        # Partial index: "which incidents are open" is the alerting query and
        # stays fast no matter how much resolved history accumulates.
        Index(
            "ix_incident_open",
            "target_id",
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("ix_incident_started", "started_at"),
    )
