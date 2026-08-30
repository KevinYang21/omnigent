"""Extend message-event receipts with ownership and native pending input.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add lease state and durable native pending-message metadata."""
    with op.batch_alter_table("message_event_receipts") as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("pending_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("pending_payload", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("pending_created_by", sa.String(length=128), nullable=True))

    # Rows written by the first draft migration predate ownership. Treat an
    # in-flight row as expired rather than granting a fresh dispatch lease.
    op.execute(
        sa.text(
            "UPDATE message_event_receipts "
            "SET owner_id = 'legacy', lease_expires_at = updated_at"
        )
    )

    with op.batch_alter_table("message_event_receipts") as batch_op:
        batch_op.alter_column("owner_id", existing_type=sa.String(length=64), nullable=False)
        batch_op.alter_column("lease_expires_at", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_constraint("ck_message_event_receipts_status", type_="check")
        batch_op.drop_constraint("ck_message_event_receipts_outcome", type_="check")
        batch_op.create_check_constraint(
            "ck_message_event_receipts_status",
            "status IN ('pending', 'completed', 'failed', 'uncertain')",
        )
        batch_op.create_check_constraint(
            "ck_message_event_receipts_outcome",
            "(status = 'completed' AND outcome IS NOT NULL) OR "
            "(status IN ('pending', 'failed', 'uncertain') AND outcome IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_message_event_receipts_pending_payload",
            "(pending_id IS NULL AND pending_payload IS NULL) OR "
            "(pending_id IS NOT NULL AND pending_payload IS NOT NULL)",
        )


def downgrade() -> None:
    """Remove lease and durable native pending-message metadata."""
    # The old schema has no uncertain state. Preserve fail-closed behavior by
    # mapping uncertain rows back to orphaned pending receipts.
    op.execute(
        sa.text(
            "UPDATE message_event_receipts SET status = 'pending' "
            "WHERE status = 'uncertain'"
        )
    )
    with op.batch_alter_table("message_event_receipts") as batch_op:
        batch_op.drop_constraint("ck_message_event_receipts_pending_payload", type_="check")
        batch_op.drop_constraint("ck_message_event_receipts_outcome", type_="check")
        batch_op.drop_constraint("ck_message_event_receipts_status", type_="check")
        batch_op.create_check_constraint(
            "ck_message_event_receipts_status",
            "status IN ('pending', 'completed', 'failed')",
        )
        batch_op.create_check_constraint(
            "ck_message_event_receipts_outcome",
            "(status = 'completed' AND outcome IS NOT NULL) OR "
            "(status IN ('pending', 'failed') AND outcome IS NULL)",
        )
        batch_op.drop_column("pending_created_by")
        batch_op.drop_column("pending_payload")
        batch_op.drop_column("pending_id")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("owner_id")
