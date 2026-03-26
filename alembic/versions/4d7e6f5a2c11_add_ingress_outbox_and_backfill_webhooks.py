"""add ingress/outbox tables and backfill webhooks if missing

Revision ID: 4d7e6f5a2c11
Revises: b2f1c3d4e5a6
Create Date: 2026-03-26 01:20:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4d7e6f5a2c11"
down_revision: Union[str, Sequence[str], None] = "b2f1c3d4e5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type(dialect_name: str):
    if dialect_name == "postgresql":
        from sqlalchemy.dialects import postgresql

        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _uuid_type(dialect_name: str):
    if dialect_name == "postgresql":
        from sqlalchemy.dialects import postgresql

        return postgresql.UUID(as_uuid=False)
    return sa.String()


def _current_timestamp(dialect_name: str):
    if dialect_name == "sqlite":
        return sa.text("(CURRENT_TIMESTAMP)")
    return sa.text("CURRENT_TIMESTAMP")


def _create_webhooks_if_missing(dialect_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("webhooks"):
        return

    op.create_table(
        "webhooks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), server_default="", nullable=False),
        sa.Column("event_types", _json_type(dialect_name), server_default="[]", nullable=False),
        sa.Column("source_filter", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_current_timestamp(dialect_name)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    inspector = sa.inspect(bind)

    if not inspector.has_table("ingress_events"):
        op.create_table(
            "ingress_events",
            sa.Column("id", _uuid_type(dialect_name), nullable=False),
            sa.Column("source_id", sa.String(), nullable=False),
            sa.Column("type", sa.String(), nullable=False),
            sa.Column("data", _json_type(dialect_name), server_default="{}", nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=_current_timestamp(dialect_name), nullable=False),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(), server_default="accepted", nullable=False),
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=_current_timestamp(dialect_name), nullable=False),
            sa.Column("lease_owner", sa.String(), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), server_default="", nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source_id", "idempotency_key", name="uq_ingress_source_idempotency"),
        )
        op.create_index("idx_ingress_status_next_attempt", "ingress_events", ["status", "next_attempt_at"], unique=False)
        op.create_index("idx_ingress_lease_until", "ingress_events", ["lease_until"], unique=False)
        op.create_index("idx_ingress_accepted_at", "ingress_events", ["accepted_at"], unique=False)

    if not inspector.has_table("outbox_events"):
        op.create_table(
            "outbox_events",
            sa.Column("id", _uuid_type(dialect_name), nullable=False),
            sa.Column("event_id", _uuid_type(dialect_name), nullable=False),
            sa.Column("source_id", sa.String(), nullable=False),
            sa.Column("type", sa.String(), nullable=False),
            sa.Column("data", _json_type(dialect_name), server_default="{}", nullable=False),
            sa.Column("event_ts", sa.DateTime(timezone=True), server_default=_current_timestamp(dialect_name), nullable=False),
            sa.Column("pipeline", sa.String(), nullable=False),
            sa.Column("status", sa.String(), server_default="pending", nullable=False),
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=_current_timestamp(dialect_name), nullable=False),
            sa.Column("lease_owner", sa.String(), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), server_default="", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=_current_timestamp(dialect_name), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id", "pipeline", name="uq_outbox_event_pipeline"),
        )
        op.create_index(
            "idx_outbox_pipeline_status_next_attempt",
            "outbox_events",
            ["pipeline", "status", "next_attempt_at"],
            unique=False,
        )
        op.create_index("idx_outbox_lease_until", "outbox_events", ["lease_until"], unique=False)

    _create_webhooks_if_missing(dialect_name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("outbox_events"):
        op.drop_index("idx_outbox_lease_until", table_name="outbox_events")
        op.drop_index("idx_outbox_pipeline_status_next_attempt", table_name="outbox_events")
        op.drop_table("outbox_events")

    if inspector.has_table("ingress_events"):
        op.drop_index("idx_ingress_accepted_at", table_name="ingress_events")
        op.drop_index("idx_ingress_lease_until", table_name="ingress_events")
        op.drop_index("idx_ingress_status_next_attempt", table_name="ingress_events")
        op.drop_table("ingress_events")
