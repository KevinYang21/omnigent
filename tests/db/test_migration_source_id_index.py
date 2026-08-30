"""DDL coverage for the conversation-item source identity migration."""

from __future__ import annotations

import io
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

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


class _FakeMappings:
    """Minimal mapping result used by the restart simulation."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _FakeMappings:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _RestartablePostgresOperations:
    """Stateful Alembic-operation fake that leaves an invalid first index."""

    def __init__(self) -> None:
        self.column_exists = False
        self.index: dict[str, Any] | None = None
        self.create_attempts = 0
        self.drop_attempts = 0
        self.context = SimpleNamespace(
            as_sql=False,
            dialect=SimpleNamespace(name="postgresql"),
            autocommit_block=nullcontext,
        )
        self.bind = SimpleNamespace(execute=self._catalog_query)

    def get_context(self) -> Any:
        return self.context

    def get_bind(self) -> Any:
        return self.bind

    def add_column(self, table: str, column: Any) -> None:
        assert table == "conversation_items"
        assert column.name == "source_id"
        assert not self.column_exists
        self.column_exists = True

    def execute(self, statement: str) -> None:
        if statement.startswith("DROP INDEX CONCURRENTLY"):
            self.drop_attempts += 1
            self.index = None
            return
        assert statement.startswith("CREATE INDEX CONCURRENTLY")
        self.create_attempts += 1
        definition = (
            "CREATE INDEX ix_conversation_items_source_id ON conversation_items "
            "USING btree (workspace_id, conversation_id, source_id) "
            "WHERE (source_id IS NOT NULL)"
        )
        if self.create_attempts == 1:
            self.index = {"indisvalid": False, "definition": definition}
            raise RuntimeError("concurrent build cancelled")
        self.index = {"indisvalid": True, "definition": definition}

    def _catalog_query(self, statement: Any, params: dict[str, str]) -> _FakeMappings:
        assert "pg_get_indexdef" in str(statement)
        assert params["name"] == "ix_conversation_items_source_id"
        return _FakeMappings(self.index)


def test_postgresql_migration_recovers_after_cancelled_index_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed column and invalid index are repaired on migration retry."""
    operations = _RestartablePostgresOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        migration,
        "_source_id_column_exists",
        lambda: operations.column_exists,
    )
    stamped = False

    with pytest.raises(RuntimeError, match="cancelled"):
        migration.upgrade()
        stamped = True

    assert operations.column_exists is True
    assert operations.index is not None
    assert operations.index["indisvalid"] is False
    assert stamped is False

    migration.upgrade()
    stamped = True

    assert operations.create_attempts == 2
    assert operations.drop_attempts == 1
    assert operations.index is not None
    assert operations.index["indisvalid"] is True
    assert stamped is True
