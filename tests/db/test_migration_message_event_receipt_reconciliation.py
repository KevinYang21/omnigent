"""Migration checks for durable message-event reconciliation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    uri = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_previous_binary_can_insert_old_receipt_shape_after_upgrade(
    db_engine: Engine,
) -> None:
    """New ownership columns remain nullable during rolling deploy/rollback."""
    columns = {
        column["name"]: column
        for column in sa.inspect(db_engine).get_columns("message_event_receipts")
    }
    assert columns["owner_id"]["nullable"] is True
    assert columns["lease_expires_at"]["nullable"] is True

    with db_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO message_event_receipts "
                "(workspace_id, conversation_id, client_event_id, fingerprint, "
                "status, outcome, created_at, updated_at) "
                "VALUES (0, :conversation_id, :client_event_id, :fingerprint, "
                "'pending', NULL, 1, 1)"
            ),
            {
                "conversation_id": bytes.fromhex("11" * 16),
                "client_event_id": "old-binary-event",
                "fingerprint": bytes.fromhex("22" * 32),
            },
        )
        row = connection.execute(
            sa.text(
                "SELECT owner_id, lease_expires_at FROM message_event_receipts "
                "WHERE client_event_id = 'old-binary-event'"
            )
        ).one()
    assert (row.owner_id, row.lease_expires_at) == (None, None)


def test_reconciliation_columns_have_compatible_defaults(db_engine: Engine) -> None:
    conversation_columns = {
        column["name"]: column
        for column in sa.inspect(db_engine).get_columns("conversations")
    }
    assert conversation_columns["next_message_event_sequence"]["nullable"] is False
    assert str(conversation_columns["next_message_event_sequence"]["default"]) in {"0", "'0'"}
