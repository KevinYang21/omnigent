"""add source_id to conversation_items

Revision ID: f3c7a1d9e2b4
Revises: e5d9bc8ac650
Create Date: 2026-08-30 00:00:00.000000

Persists a native transcript record's stable source identity so an
``external_conversation_item`` retry after a forwarder crash can resolve the
already-committed item instead of appending a duplicate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c7a1d9e2b4"
down_revision: str | None = "e5d9bc8ac650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_conversation_items_source_id"


def upgrade() -> None:
    """Add the nullable source identity and its lookup index."""
    op.add_column(
        "conversation_items",
        sa.Column("source_id", sa.String(length=512), nullable=True),
    )
    op.create_index(
        _INDEX_NAME,
        "conversation_items",
        ["workspace_id", "conversation_id", "source_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the source identity lookup and column."""
    op.drop_index(_INDEX_NAME, table_name="conversation_items")
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.drop_column("source_id")
