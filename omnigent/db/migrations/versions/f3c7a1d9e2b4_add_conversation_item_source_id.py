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


def _drop_postgresql_index_if_invalid() -> None:
    """Remove an unusable remnant from a failed concurrent index build."""
    if op.get_context().as_sql:
        return
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": _INDEX_NAME},
        )
        .scalar()
    )
    if invalid:
        op.execute(f"DROP INDEX {_INDEX_NAME}")


def upgrade() -> None:
    """Add the nullable source identity and its lookup index."""
    op.add_column(
        "conversation_items",
        sa.Column("source_id", sa.String(length=512), nullable=True),
    )
    dialect_name = op.get_context().dialect.name
    if dialect_name == "postgresql":
        # A production conversation_items table can be large. PostgreSQL's
        # regular CREATE INDEX blocks writes, while CONCURRENTLY is illegal
        # inside Alembic's migration transaction.
        with op.get_context().autocommit_block():
            _drop_postgresql_index_if_invalid()
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
                "ON conversation_items (workspace_id, conversation_id, source_id) "
                "WHERE source_id IS NOT NULL"
            )
    elif dialect_name == "sqlite":
        op.create_index(
            _INDEX_NAME,
            "conversation_items",
            ["workspace_id", "conversation_id", "source_id"],
            unique=False,
            sqlite_where=sa.text("source_id IS NOT NULL"),
        )
    else:
        op.create_index(
            _INDEX_NAME,
            "conversation_items",
            ["workspace_id", "conversation_id", "source_id"],
            unique=False,
        )


def downgrade() -> None:
    """Remove the source identity lookup and column."""
    if op.get_context().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    else:
        op.drop_index(_INDEX_NAME, table_name="conversation_items")
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.drop_column("source_id")
