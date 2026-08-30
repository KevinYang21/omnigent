"""Regression tests for versioned split conversation-database upgrades."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from omnigent.db import ConversationBase
from omnigent.db.utils import _run_conversation_schema_upgrades, clear_engine_cache
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def test_preexisting_split_database_upgrades_before_append(tmp_path: Path) -> None:
    """A legacy split DB gains source identity before normal store writes."""
    main_uri = f"sqlite:///{tmp_path / 'main.db'}"
    split_uri = f"sqlite:///{tmp_path / 'conversations.db'}"
    legacy_engine = sa.create_engine(split_uri)
    ConversationBase.metadata.create_all(legacy_engine)
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_conversation_items_source_id")
        connection.exec_driver_sql("ALTER TABLE conversation_items DROP COLUMN source_id")
    assert "source_id" not in {
        column["name"] for column in sa.inspect(legacy_engine).get_columns("conversation_items")
    }
    legacy_engine.dispose()

    clear_engine_cache()
    store = SqlAlchemyConversationStore(main_uri, split_uri)
    conversation = store.create_conversation()
    persisted = store.append(
        conversation.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_split_upgrade",
                source_id="legacy-split-source:0:message",
                data=MessageData(
                    role="assistant",
                    agent="claude-code",
                    content=[{"type": "output_text", "text": "after split upgrade"}],
                ),
            )
        ],
    )

    assert len(persisted) == 1
    _run_conversation_schema_upgrades(store._conv_engine)
    inspector = sa.inspect(store._conv_engine)
    assert "source_id" in {
        column["name"] for column in inspector.get_columns("conversation_items")
    }
    assert "ix_conversation_items_source_id" in {
        index["name"] for index in inspector.get_indexes("conversation_items")
    }
    assert "omnigent_conversation_schema_migrations" in inspector.get_table_names()
    with store._conv_engine.connect() as connection:
        versions = connection.execute(
            sa.text("SELECT version FROM omnigent_conversation_schema_migrations")
        ).scalars()
        assert list(versions) == [1]
