"""DDL coverage for the conversation-item source identity migration."""

from __future__ import annotations

import io

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.migrations.versions import (
    f3c7a1d9e2b4_add_conversation_item_source_id as migration,
)


def _compile_upgrade(monkeypatch: pytest.MonkeyPatch, dialect_name: str) -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name=dialect_name,
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    with context.begin_transaction():
        migration.upgrade()
    return output.getvalue()


def test_postgresql_source_index_is_concurrent_partial_and_autocommitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL must not build the large lookup index in a transaction."""
    ddl = _compile_upgrade(monkeypatch, "postgresql")

    index = ddl.index("CREATE INDEX CONCURRENTLY")
    assert ddl.rfind("COMMIT", 0, index) >= 0
    assert "WHERE source_id IS NOT NULL" in ddl[index:]


@pytest.mark.parametrize(
    ("dialect_name", "partial"),
    [("sqlite", True), ("mysql", False)],
)
def test_source_index_ddl_remains_portable(
    monkeypatch: pytest.MonkeyPatch,
    dialect_name: str,
    partial: bool,
) -> None:
    """SQLite gets its supported partial form; MySQL gets a plain index."""
    ddl = _compile_upgrade(monkeypatch, dialect_name)

    assert "CREATE INDEX ix_conversation_items_source_id" in ddl
    assert ("WHERE source_id IS NOT NULL" in ddl) is partial
    assert "CONCURRENTLY" not in ddl
