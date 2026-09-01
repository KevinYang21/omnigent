"""add replica_url to hosts

Revision ID: hb2c3d4e5f6a7
Revises: ga1b2c3d4e5f
Create Date: 2026-09-01 00:00:00.000000

Adds ``hosts.replica_url`` — the advertise URL of the server replica holding
the host's live WebSocket tunnel, written on every tunnel connect. A replica
that receives a session request for a host it doesn't hold (a wrong-replica
routing miss) reads this column to forward the request server-side to the
replica that can serve it, instead of stranding the client on a terminal
``wrong_replica`` error. ``NULL`` means the holding replica did not advertise
a peer-reachable URL (single-replica deploys, wildcard binds without an
explicit advertise URL) — forwarding is then unavailable and the miss
surfaces as before.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "hb2c3d4e5f6a7"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``replica_url`` column to ``hosts``.

    Batch mode so the DDL runs on SQLite too, and so the project's
    migration-safety test (which requires every schema change to go
    through ``batch_alter_table``) passes.
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("replica_url", sa.String(256), nullable=True))


def downgrade() -> None:
    """Drop the ``replica_url`` column from ``hosts``."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("replica_url")
