"""Harden native receipt reconciliation and rolling compatibility.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PENDING_METADATA_TTL_SECONDS = 24 * 60 * 60


def upgrade() -> None:
    """Add deterministic reconciliation state without breaking old writers."""
    uuid_type = sa.LargeBinary(length=16).with_variant(MySQLBinary(16), "mysql")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "next_message_event_sequence",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
    with op.batch_alter_table("message_event_receipts") as batch_op:
        batch_op.drop_constraint("ck_message_event_receipts_pending_payload", type_="check")
        batch_op.alter_column("owner_id", existing_type=sa.String(length=64), nullable=True)
        batch_op.alter_column("lease_expires_at", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("pending_sequence", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("pending_metadata_expires_at", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("committed_item_id", uuid_type, nullable=True))

    # Draft deployments may already contain pending metadata. Backfill a stable
    # order before enforcing the all-or-none metadata constraint.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT workspace_id, conversation_id, client_event_id, updated_at "
            "FROM message_event_receipts WHERE pending_id IS NOT NULL "
            "ORDER BY workspace_id, conversation_id, created_at, client_event_id"
        )
    ).fetchall()
    counters: dict[tuple[object, object], int] = {}
    for workspace_id, conversation_id, client_event_id, updated_at in rows:
        key = (workspace_id, conversation_id)
        sequence = counters.get(key, 0) + 1
        counters[key] = sequence
        connection.execute(
            sa.text(
                "UPDATE message_event_receipts "
                "SET pending_sequence = :sequence, pending_metadata_expires_at = :expires "
                "WHERE workspace_id = :workspace_id "
                "AND conversation_id = :conversation_id "
                "AND client_event_id = :client_event_id"
            ),
            {
                "sequence": sequence,
                "expires": int(updated_at) + _PENDING_METADATA_TTL_SECONDS,
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "client_event_id": client_event_id,
            },
        )
    for (workspace_id, conversation_id), sequence in counters.items():
        connection.execute(
            sa.text(
                "UPDATE conversations SET next_message_event_sequence = :sequence "
                "WHERE workspace_id = :workspace_id AND id = :conversation_id"
            ),
            {
                "sequence": sequence,
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
            },
        )

    with op.batch_alter_table("message_event_receipts") as batch_op:
        batch_op.create_check_constraint(
            "ck_message_event_receipts_pending_payload",
            "(pending_id IS NULL AND pending_payload IS NULL "
            "AND pending_sequence IS NULL AND pending_metadata_expires_at IS NULL) OR "
            "(pending_id IS NOT NULL AND pending_payload IS NOT NULL "
            "AND pending_sequence IS NOT NULL AND pending_metadata_expires_at IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_message_event_receipts_pending_sequence",
            ["workspace_id", "conversation_id", "pending_sequence"],
            unique=False,
        )
        batch_op.create_index(
            "ix_message_event_receipts_pending_expiry",
            ["workspace_id", "pending_metadata_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    """Remove hardened reconciliation state while preserving old-writer safety."""
    with op.batch_alter_table("message_event_receipts") as batch_op:
        batch_op.drop_index("ix_message_event_receipts_pending_expiry")
        batch_op.drop_index("ix_message_event_receipts_pending_sequence")
        batch_op.drop_constraint("ck_message_event_receipts_pending_payload", type_="check")
        batch_op.create_check_constraint(
            "ck_message_event_receipts_pending_payload",
            "(pending_id IS NULL AND pending_payload IS NULL) OR "
            "(pending_id IS NOT NULL AND pending_payload IS NOT NULL)",
        )
        batch_op.drop_column("committed_item_id")
        batch_op.drop_column("pending_metadata_expires_at")
        batch_op.drop_column("pending_sequence")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("next_message_event_sequence")
