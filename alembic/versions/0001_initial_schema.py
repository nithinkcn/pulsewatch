"""Initial schema: target, check, incident.

Revision ID: 0001
Revises:
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enums are stored as VARCHAR + CHECK rather than native PostgreSQL enums.
# Adding a value to a native enum needs ALTER TYPE, which historically could
# not run inside a transaction — an avoidable hazard in an automated deploy.
TARGET_STATUS = sa.Enum("UNKNOWN", "UP", "DOWN", name="target_status", native_enum=False, length=16)
CHECK_TYPE = sa.Enum("HTTP", "TCP", name="check_type", native_enum=False, length=16)


def upgrade() -> None:
    op.create_table(
        "target",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("group", sa.String(200), nullable=True),
        sa.Column("check_type", CHECK_TYPE, nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("expected_status", sa.Integer(), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("failure_threshold", sa.Integer(), nullable=False),
        sa.Column("recovery_threshold", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", TARGET_STATUS, nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("consecutive_successes", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("interval_seconds >= 5", name="ck_target_interval_min"),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_target_timeout_positive"),
        sa.CheckConstraint("failure_threshold >= 1", name="ck_target_failure_threshold"),
        sa.CheckConstraint("recovery_threshold >= 1", name="ck_target_recovery_threshold"),
    )
    op.create_index("ix_target_group", "target", ["group"])
    op.create_index("ix_target_due", "target", ["enabled", "last_checked_at"])

    op.create_table(
        "check",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["target_id"], ["target.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_check_target_time", "check", ["target_id", "checked_at"])

    op.create_table(
        "incident",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cause", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["target_id"], ["target.id"], ondelete="CASCADE"),
    )
    # Partial index — "what is broken right now" stays O(open incidents),
    # not O(every outage ever recorded).
    op.create_index(
        "ix_incident_open",
        "incident",
        ["target_id"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index("ix_incident_started", "incident", ["started_at"])


def downgrade() -> None:
    op.drop_table("incident")
    op.drop_table("check")
    op.drop_table("target")
