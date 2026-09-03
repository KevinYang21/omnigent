"""Interactive Omnigent session picker for a Herdr popup pane."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, TypeAlias

PLUGIN_ROOT = Path(__file__).resolve().parent
BRIDGE_PATH = PLUGIN_ROOT / "session_space_sync.py"
SCOPES = ("all", "active", "pinned")
CACHE_VERSION = 1
CACHE_MAX_BYTES = 5_000_000
CACHE_MAX_RECORDS = 2_000
CACHE_MAX_AGE_SECONDS = 7 * 86_400
AGENT_CACHE_MAX_RECORDS = 100
AGENT_CACHE_MAX_AGE_SECONDS = 30 * 86_400
Scope: TypeAlias = Literal["all", "active", "pinned"]
NewSessionPhase: TypeAlias = Literal[
    "browse",
    "loading_agents",
    "choose_agent",
    "compose_message",
    "agent_error",
    "creating",
    "sending",
    "send_unknown",
    "opening",
    "create_failed",
    "open_failed",
]


class PickerError(RuntimeError):
    """The picker configuration or bridge command failed."""


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return cleaned or None


def _required_text(value: object, field_name: str) -> str:
    cleaned = _optional_text(value)
    if cleaned is None:
        raise PickerError(f"session record has no valid {field_name}")
    return cleaned


def _normalize_query(value: str) -> str:
    return " ".join(value.casefold().split())


def _initial_prompt_label(value: str) -> str:
    """Return a short provisional label without flattening the prompt itself."""
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    return (_optional_text(first_line) or "New Omnigent session")[:80]


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    name: str
    display_name: str
    harness: str | None

    @classmethod
    def from_json(cls, value: object) -> AgentRecord:
        if not isinstance(value, dict):
            raise PickerError("agent catalog contains a non-object record")
        agent_id = _optional_text(value.get("id"))
        name = _optional_text(value.get("name"))
        if agent_id is None:
            raise PickerError("agent record has no valid id")
        if name is None:
            raise PickerError("agent record has no valid name")
        return cls(
            agent_id=agent_id,
            name=name,
            display_name=_optional_text(value.get("display_name")) or name,
            harness=_optional_text(value.get("harness")),
        )

    @property
    def search_text(self) -> str:
        return _normalize_query(
            " ".join(
                value
                for value in (self.display_name, self.name, self.harness)
                if value is not None
            )
        )

    @property
    def preferred_codex(self) -> bool:
        return (
            self.name.casefold() == "codex-native-ui"
            or (self.harness or "").casefold() == "codex-native"
            or self.display_name.casefold() == "codex"
        )


class PersistentAgentCache:
    """Small server-scoped snapshot for an instant new-session chooser."""

    def __init__(self, path: Path, *, scope: str) -> None:
        self.path = path
        self.scope = scope

    def load(self) -> list[AgentRecord] | None:
        try:
            if self.path.stat().st_size > CACHE_MAX_BYTES:
                return None
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or value.get("version") != CACHE_VERSION
                or value.get("scope") != self.scope
            ):
                return None
            saved_at = value.get("saved_at")
            if not isinstance(saved_at, int) or isinstance(saved_at, bool):
                return None
            age = int(time.time()) - saved_at
            if age > AGENT_CACHE_MAX_AGE_SECONDS or age < -86_400:
                return None
            raw_agents = value.get("agents")
            if not isinstance(raw_agents, list) or len(raw_agents) > AGENT_CACHE_MAX_RECORDS:
                return None
            return [AgentRecord.from_json(item) for item in raw_agents]
        except (OSError, UnicodeError, json.JSONDecodeError, PickerError):
            return None

    def store(self, agents: Sequence[AgentRecord]) -> None:
        if len(agents) > AGENT_CACHE_MAX_RECORDS:
            return
        document = {
            "version": CACHE_VERSION,
            "scope": self.scope,
            "saved_at": int(time.time()),
            "agents": [
                {
                    "id": agent.agent_id,
                    "name": agent.name,
                    "display_name": agent.display_name,
                    "harness": agent.harness,
                }
                for agent in agents
            ],
        }
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(document, temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError:
            return
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink()


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    title: str | None
    agent: str | None
    status: Literal["idle", "running", "waiting", "failed"]
    active: bool
    attention: bool
    runner_online: bool
    recovery_hint: str
    pinned: bool
    project: str | None
    workspace: str | None
    updated_at: int | None
    loaded: bool
    search_snippet: str | None = None

    @classmethod
    def from_json(cls, value: object) -> SessionRecord:
        if not isinstance(value, dict):
            raise PickerError("session catalog contains a non-object record")
        status = value.get("status")
        if status not in {"idle", "running", "waiting", "failed"}:
            raise PickerError(f"session record has unknown status {status!r}")
        updated_at = value.get("updated_at")
        return cls(
            session_id=_required_text(value.get("id"), "id"),
            title=_optional_text(value.get("title")),
            agent=_optional_text(value.get("agent")),
            status=status,
            active=value.get("active") is True,
            attention=value.get("attention") is True,
            runner_online=value.get("runner_online") is True,
            recovery_hint=_optional_text(value.get("recovery_hint")) or "unknown",
            pinned=value.get("pinned") is True,
            project=_optional_text(value.get("project")),
            workspace=_optional_text(value.get("workspace")),
            updated_at=(
                updated_at
                if isinstance(updated_at, int) and not isinstance(updated_at, bool)
                else None
            ),
            loaded=value.get("loaded") is True,
            search_snippet=_optional_text(value.get("search_snippet")),
        )

    def to_json(
        self,
        *,
        include_search_snippet: bool = True,
        include_workspace: bool = True,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "id": self.session_id,
            "title": self.title,
            "agent": self.agent,
            "status": self.status,
            "active": self.active,
            "attention": self.attention,
            "runner_online": self.runner_online,
            "recovery_hint": self.recovery_hint,
            "pinned": self.pinned,
            "project": self.project,
            "updated_at": self.updated_at,
            "loaded": self.loaded,
        }
        if include_workspace:
            value["workspace"] = self.workspace
        if include_search_snippet:
            value["search_snippet"] = self.search_snippet
        return value

    @property
    def label(self) -> str:
        return self.title or self.agent or self.session_id

    @property
    def rank_key(self) -> tuple[bool, bool, bool, bool, bool, int, str]:
        """Stable default order: intent and attention before recency."""
        return (
            not self.pinned,
            not self.loaded,
            not self.attention,
            not self.active,
            not self.runner_online,
            -(self.updated_at or 0),
            self.session_id,
        )


class PersistentCatalogCache:
    """Best-effort base catalog snapshot for instant popup startup."""

    def __init__(self, path: Path, *, scope: str | None = None) -> None:
        self.path = path
        self.scope = scope or hashlib.sha256(str(path).encode()).hexdigest()

    def load(self) -> list[SessionRecord] | None:
        try:
            if self.path.stat().st_size > CACHE_MAX_BYTES:
                return None
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or value.get("version") != CACHE_VERSION
                or value.get("scope") != self.scope
            ):
                return None
            saved_at = value.get("saved_at")
            if not isinstance(saved_at, int) or isinstance(saved_at, bool):
                return None
            age = int(time.time()) - saved_at
            if age > CACHE_MAX_AGE_SECONDS or age < -86_400:
                return None
            raw_records = value.get("records")
            if not isinstance(raw_records, list) or len(raw_records) > CACHE_MAX_RECORDS:
                return None
            return [SessionRecord.from_json(record) for record in raw_records]
        except (OSError, UnicodeError, json.JSONDecodeError, PickerError):
            return None

    def store(self, records: Sequence[SessionRecord]) -> None:
        if len(records) > CACHE_MAX_RECORDS:
            return
        document = {
            "version": CACHE_VERSION,
            "scope": self.scope,
            "saved_at": int(time.time()),
            "records": [
                record.to_json(
                    include_search_snippet=False,
                    include_workspace=False,
                )
                for record in records
            ],
        }
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(document, temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            if temporary_path.stat().st_size > CACHE_MAX_BYTES:
                return
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError:
            return
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink()


class CatalogCache:
    """Small per-popup cache with instant filtering from the base catalog."""

    def __init__(self, max_entries: int = 32) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[SessionRecord, ...]] = OrderedDict()
        self._base_records: tuple[SessionRecord, ...] | None = None

    def store(self, query: str, records: Sequence[SessionRecord]) -> None:
        normalized_query = _normalize_query(query)
        cached = tuple(records)
        self._entries.pop(normalized_query, None)
        self._entries[normalized_query] = cached
        if normalized_query == "":
            self._base_records = cached
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def contains(self, query: str) -> bool:
        normalized_query = _normalize_query(query)
        if normalized_query == "" and self._base_records is not None:
            return True
        return normalized_query in self._entries

    def lookup(self, query: str) -> list[SessionRecord] | None:
        normalized_query = _normalize_query(query)
        exact = self._entries.get(normalized_query)
        if exact is not None:
            self._entries.move_to_end(normalized_query)
            return list(exact)
        if self._base_records is None:
            return None
        tokens = normalized_query.split()
        return [record for record in self._base_records if self._matches(record, tokens)]

    def upsert(self, record: SessionRecord) -> None:
        """Insert one fresh session into every compatible cached catalog."""
        if "" not in self._entries:
            previous_base = self._base_records or ()
            self.store(
                "",
                [
                    record,
                    *(item for item in previous_base if item.session_id != record.session_id),
                ],
            )
        for query, records in list(self._entries.items()):
            remaining = tuple(item for item in records if item.session_id != record.session_id)
            include = query == "" or self._matches(record, query.split())
            self._entries[query] = (record, *remaining) if include else remaining
        self._base_records = self._entries[""]

    def base_records(self) -> list[SessionRecord] | None:
        if self._base_records is None:
            return None
        return list(self._base_records)

    @classmethod
    def _matches(cls, record: SessionRecord, tokens: Sequence[str]) -> bool:
        search_text = cls._search_text(record)
        return all(token in search_text for token in tokens)

    @staticmethod
    def _search_text(record: SessionRecord) -> str:
        return _normalize_query(
            " ".join(
                value
                for value in (
                    record.label,
                    record.session_id,
                    record.agent,
                    record.project,
                    record.workspace,
                    record.search_snippet,
                )
                if value
            )
        )


def _merge_created_records(
    records: Sequence[SessionRecord],
    created_records: Sequence[SessionRecord],
    *,
    query: str,
) -> list[SessionRecord]:
    """Keep unseen local rows while preferring authoritative server metadata."""
    merged = list(records)
    tokens = _normalize_query(query).split()
    for created in created_records:
        server_index = next(
            (
                index
                for index, record in enumerate(merged)
                if record.session_id == created.session_id
            ),
            None,
        )
        if server_index is not None:
            server_record = merged[server_index]
            merged[server_index] = replace(
                server_record,
                loaded=server_record.loaded or created.loaded,
            )
            continue
        if not tokens or CatalogCache._matches(created, tokens):
            merged.insert(0, created)
    return merged


@dataclass
class PickerState:
    records: list[SessionRecord] = field(default_factory=list)
    scope: Scope = "all"
    selected_index: int = 0
    query: str = ""
    loading: bool = False
    opening: bool = False
    error: str | None = None
    request_generation: int = 0
    displayed_query: str | None = None

    @property
    def visible_records(self) -> list[SessionRecord]:
        if self.scope == "active":
            return [record for record in self.records if record.active or record.loaded]
        if self.scope == "pinned":
            return [record for record in self.records if record.pinned]
        return self.records

    @property
    def selected(self) -> SessionRecord | None:
        visible = self.visible_records
        if not visible:
            return None
        return visible[min(self.selected_index, len(visible) - 1)]

    @property
    def can_open(self) -> bool:
        return (
            self.selected is not None
            and not self.loading
            and not self.opening
            and self.error is None
        )

    def set_records(
        self,
        records: Sequence[SessionRecord],
        *,
        preserve_selection: bool = True,
    ) -> None:
        selected_id = (
            self.selected.session_id if preserve_selection and self.selected is not None else None
        )
        self.records = sorted(records, key=lambda record: record.rank_key)
        visible = self.visible_records
        if selected_id is not None:
            selected_index = next(
                (
                    index
                    for index, record in enumerate(visible)
                    if record.session_id == selected_id
                ),
                None,
            )
            if selected_index is not None:
                self.selected_index = selected_index
                return
        if preserve_selection:
            self.selected_index = min(self.selected_index, max(0, len(visible) - 1))
        else:
            self.selected_index = 0

    def move(self, delta: int) -> None:
        visible = self.visible_records
        if not visible:
            self.selected_index = 0
            return
        self.selected_index = min(
            len(visible) - 1,
            max(0, self.selected_index + delta),
        )

    def cycle_scope(self, delta: int = 1) -> None:
        current = SCOPES.index(self.scope)
        self.scope = SCOPES[(current + delta) % len(SCOPES)]  # type: ignore[assignment]
        self.selected_index = 0

    def count(self, scope: Scope) -> int:
        if scope == "active":
            return sum(record.active or record.loaded for record in self.records)
        if scope == "pinned":
            return sum(record.pinned for record in self.records)
        return len(self.records)

    def select_session(self, session_id: str | None) -> None:
        if session_id is None:
            return
        selected_index = next(
            (
                index
                for index, record in enumerate(self.visible_records)
                if record.session_id == session_id
            ),
            None,
        )
        if selected_index is not None:
            self.selected_index = selected_index


@dataclass
class NewSessionState:
    phase: NewSessionPhase = "browse"
    agents: list[AgentRecord] = field(default_factory=list)
    query: str = ""
    selected_index: int = 0
    error: str | None = None
    prompt: str = ""
    chosen_agent: AgentRecord | None = None
    created_record: SessionRecord | None = None
    browse_query: str = ""
    browse_selected_session_id: str | None = None
    agent_request_generation: int = 0
    agent_loading: bool = False

    @property
    def active(self) -> bool:
        return self.phase != "browse"

    @property
    def busy(self) -> bool:
        return self.phase in {"creating", "sending", "opening"}

    @property
    def visible_agents(self) -> list[AgentRecord]:
        tokens = _normalize_query(self.query).split()
        if not tokens:
            return self.agents
        return [
            agent for agent in self.agents if all(token in agent.search_text for token in tokens)
        ]

    @property
    def selected_agent(self) -> AgentRecord | None:
        visible = self.visible_agents
        if not visible:
            return None
        return visible[min(self.selected_index, len(visible) - 1)]

    def enter(self, browse_query: str, browse_selected_session_id: str | None) -> bool:
        if self.active:
            return False
        self.phase = "loading_agents"
        self.agents = []
        self.query = ""
        self.selected_index = 0
        self.error = None
        self.prompt = ""
        self.chosen_agent = None
        self.created_record = None
        self.agent_loading = False
        self.browse_query = browse_query
        self.browse_selected_session_id = browse_selected_session_id
        return True

    def leave(self) -> tuple[str, str | None] | None:
        if not self.active or self.busy:
            return None
        restore = (self.browse_query, self.browse_selected_session_id)
        self.phase = "browse"
        self.agents = []
        self.query = ""
        self.selected_index = 0
        self.error = None
        self.prompt = ""
        self.chosen_agent = None
        self.created_record = None
        self.agent_loading = False
        self.agent_request_generation += 1
        return restore

    def seed_agents(self, agents: Sequence[AgentRecord]) -> bool:
        if not self.active or self.busy or not agents:
            return False
        self.agents = list(agents)
        self.phase = "choose_agent"
        self.error = None
        self._select_default()
        return True

    def start_agent_load(self) -> int | None:
        if self.phase not in {"loading_agents", "choose_agent", "agent_error"}:
            return None
        self.agent_request_generation += 1
        self.agent_loading = True
        self.phase = "choose_agent" if self.agents else "loading_agents"
        self.error = None
        return self.agent_request_generation

    def finish_agent_load(
        self,
        generation: int,
        agents: Sequence[AgentRecord] | None,
        error: str | None,
    ) -> bool:
        if generation != self.agent_request_generation or self.phase not in {
            "loading_agents",
            "choose_agent",
            "agent_error",
        }:
            return False
        self.agent_loading = False
        if error is not None:
            if self.agents:
                self.phase = "choose_agent"
                self.error = error
                return True
            self.phase = "agent_error"
            self.error = error
            self.agents = []
            self.selected_index = 0
            return True
        self.phase = "choose_agent"
        self.error = None
        self.agents = list(agents or [])
        self._select_default()
        return True

    def set_query(self, query: str) -> None:
        self.query = query
        self._select_default()

    def set_prompt(self, prompt: str) -> None:
        self.prompt = prompt

    def move(self, delta: int) -> None:
        visible = self.visible_agents
        if not visible:
            self.selected_index = 0
            return
        self.selected_index = min(
            len(visible) - 1,
            max(0, self.selected_index + delta),
        )

    def begin_compose(self) -> AgentRecord | None:
        agent = self.selected_agent
        if self.phase != "choose_agent" or agent is None:
            return None
        self.agent_request_generation += 1
        self.agent_loading = False
        self.chosen_agent = agent
        self.phase = "compose_message"
        self.error = None
        return agent

    def return_to_agent_choice(self) -> bool:
        if self.phase != "compose_message":
            return False
        self.phase = "choose_agent"
        self.chosen_agent = None
        self.error = None
        return True

    def begin_create(self) -> tuple[AgentRecord, str] | None:
        agent = self.chosen_agent
        if self.phase != "compose_message" or agent is None or not self.prompt.strip():
            return None
        self.phase = "creating"
        self.error = None
        return agent, self.prompt

    def mark_create_failed(self, error: str) -> bool:
        if self.phase != "creating":
            return False
        self.phase = "create_failed"
        self.error = error
        return True

    def mark_created(self, record: SessionRecord) -> bool:
        if self.phase != "creating":
            return False
        self.created_record = record
        self.phase = "sending"
        self.error = None
        return True

    def mark_message_sent(self) -> SessionRecord | None:
        if self.phase != "sending" or self.created_record is None:
            return None
        self.phase = "opening"
        self.error = None
        return self.created_record

    def mark_send_unknown(self, error: str) -> bool:
        if self.phase != "sending" or self.created_record is None:
            return False
        self.phase = "send_unknown"
        self.error = error
        return True

    def begin_open_after_unknown_send(self) -> SessionRecord | None:
        if self.phase != "send_unknown" or self.created_record is None:
            return None
        self.phase = "opening"
        self.error = None
        return self.created_record

    def mark_open_failed(self, error: str) -> bool:
        if self.phase != "opening" or self.created_record is None:
            return False
        self.phase = "open_failed"
        self.error = error
        return True

    def begin_open_retry(self) -> SessionRecord | None:
        if self.phase != "open_failed" or self.created_record is None:
            return None
        self.phase = "opening"
        self.error = None
        return self.created_record

    def _select_default(self) -> None:
        visible = self.visible_agents
        self.selected_index = next(
            (index for index, agent in enumerate(visible) if agent.preferred_codex),
            0,
        )


@dataclass(frozen=True)
class PickerConfig:
    server: str | None
    omnigent_executable: str
    max_sessions: int
    debounce_seconds: float
    state_file: Path
    catalog_cache_file: Path
    agent_cache_file: Path
    catalog_cache_scope: str
    fallback_cwd: Path
    herdr_executable: str

    @staticmethod
    def _resolve_auto_server_url() -> str | None:
        """Resolve the CLI-selected server for cache isolation when possible."""
        try:
            from yaml import YAMLError

            from omnigent.config import load_effective_config
        except ImportError:
            return None

        try:
            effective_config = load_effective_config()
            configured = effective_config.get("server")
            if isinstance(configured, str) and configured.strip():
                return configured

            from omnigent.host.local_server import local_server_url_if_healthy

            return local_server_url_if_healthy()
        except (ImportError, OSError, RuntimeError, TypeError, ValueError, YAMLError):
            # Discovery still reports the authoritative configuration error.
            return None

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> PickerConfig:
        env = dict(os.environ if environ is None else environ)
        config_dir = Path(env.get("HERDR_PLUGIN_CONFIG_DIR", PLUGIN_ROOT))
        config_path = config_dir / "config.json"
        document: dict[str, Any] = {}
        if config_path.exists():
            try:
                value = json.loads(config_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise PickerError(f"could not read {config_path}: {exc}") from exc
            if not isinstance(value, dict):
                raise PickerError(f"{config_path} must contain a JSON object")
            document = value

        max_sessions = document.get("max_sessions", 200)
        if (
            not isinstance(max_sessions, int)
            or isinstance(max_sessions, bool)
            or not 1 <= max_sessions <= 1000
        ):
            raise PickerError("config max_sessions must be an integer from 1 to 1000")
        debounce_ms = document.get("search_debounce_ms", 300)
        if (
            not isinstance(debounce_ms, int)
            or isinstance(debounce_ms, bool)
            or not 0 <= debounce_ms <= 5000
        ):
            raise PickerError("config search_debounce_ms must be from 0 to 5000")

        server = _optional_text(document.get("server"))
        omnigent = (
            _optional_text(document.get("omnigent")) or env.get("OMNIGENT_BIN") or "omnigent"
        )
        state_dir = Path(env.get("HERDR_PLUGIN_STATE_DIR", PLUGIN_ROOT))
        herdr_socket = env.get("HERDR_SOCKET_PATH", "")
        herdr_session = env.get("HERDR_SESSION", "")
        state_identity = herdr_socket or herdr_session or "default"
        state_override = _optional_text(document.get("state_file"))
        if state_override is not None:
            state_file = Path(state_override).expanduser()
        else:
            suffix = hashlib.sha256(state_identity.encode()).hexdigest()[:12]
            state_file = state_dir / f"session-spaces-{suffix}.json"

        cache_server = server or cls._resolve_auto_server_url()
        omnigent_server = cache_server.rstrip("/").casefold() if cache_server else "<auto>"
        config_home = env.get("OMNIGENT_CONFIG_HOME", "<default>")
        cache_identity = "\0".join((herdr_socket, herdr_session, omnigent_server, config_home))
        catalog_cache_scope = hashlib.sha256(cache_identity.encode()).hexdigest()
        cache_override = _optional_text(document.get("catalog_cache_file"))
        if cache_override is not None:
            catalog_cache_file = Path(cache_override).expanduser()
        else:
            catalog_cache_file = (
                state_dir / "catalog-cache" / f"session-catalog-{catalog_cache_scope[:12]}.json"
            )
        agent_cache_override = _optional_text(document.get("agent_cache_file"))
        if agent_cache_override is not None:
            agent_cache_file = Path(agent_cache_override).expanduser()
        else:
            agent_cache_file = (
                state_dir / "catalog-cache" / f"agent-catalog-{catalog_cache_scope[:12]}.json"
            )

        context_cwd = _context_cwd(env.get("HERDR_PLUGIN_CONTEXT_JSON"))
        configured_cwd = _optional_text(document.get("fallback_cwd"))
        fallback_cwd = Path(configured_cwd or context_cwd or os.getcwd()).expanduser()
        return cls(
            server=server,
            omnigent_executable=omnigent,
            max_sessions=max_sessions,
            debounce_seconds=debounce_ms / 1000,
            state_file=state_file,
            catalog_cache_file=catalog_cache_file,
            agent_cache_file=agent_cache_file,
            catalog_cache_scope=catalog_cache_scope,
            fallback_cwd=fallback_cwd,
            herdr_executable=env.get("HERDR_BIN_PATH", "herdr"),
        )


def _context_cwd(raw_context: str | None) -> str | None:
    if not raw_context:
        return None
    try:
        context = json.loads(raw_context)
    except json.JSONDecodeError:
        return None
    if not isinstance(context, dict):
        return None
    for key in ("focused_pane_cwd", "pane_cwd", "cwd", "workspace_cwd"):
        value = _optional_text(context.get(key))
        if value:
            return value
    pane = context.get("pane")
    if isinstance(pane, dict):
        return _optional_text(pane.get("cwd"))
    return None


class SessionSpaceBridge:
    """Argv-only process adapter around the session synchronization bridge."""

    def __init__(self, config: PickerConfig) -> None:
        self.config = config

    def _base_command(self) -> list[str]:
        command = [
            sys.executable,
            str(BRIDGE_PATH),
            "--herdr",
            self.config.herdr_executable,
            "--omnigent",
            self.config.omnigent_executable,
            "--state-file",
            str(self.config.state_file),
            "--max-sessions",
            str(self.config.max_sessions),
            "--cwd",
            str(self.config.fallback_cwd),
        ]
        if self.config.server:
            command.extend(["--server", self.config.server])
        return command

    def catalog(self, query: str) -> list[SessionRecord]:
        command = [
            *self._base_command(),
            "--list-sessions",
            "--include-pinned",
            "--include-loaded",
            "--skip-liveness",
        ]
        normalized_query = _normalize_query(query)
        if normalized_query:
            command.extend(["--search-query", normalized_query])
        completed = self._run(command, timeout=30)
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PickerError("Omnigent returned an invalid session catalog") from exc
        if not isinstance(value, list):
            raise PickerError("Omnigent returned a non-list session catalog")
        return [SessionRecord.from_json(item) for item in value]

    def list_create_agents(self) -> list[AgentRecord]:
        completed = self._run(
            [*self._base_command(), "--list-create-agents"],
            timeout=30,
        )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PickerError("Omnigent returned an invalid agent catalog") from exc
        if not isinstance(value, list):
            raise PickerError("Omnigent returned a non-list agent catalog")
        return [AgentRecord.from_json(item) for item in value]

    def create(self, agent_id: str) -> SessionRecord:
        completed = self._run(
            [*self._base_command(), "--create-session", agent_id],
            timeout=150,
        )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PickerError("Omnigent returned an invalid created session") from exc
        if not isinstance(value, dict):
            raise PickerError("Omnigent returned a non-object created session")
        return SessionRecord.from_json(value)

    def send_initial_message(self, session_id: str, prompt: str) -> None:
        self._run(
            [*self._base_command(), "--send-message", session_id],
            timeout=150,
            input_text=prompt,
        )

    def open(self, record: SessionRecord) -> None:
        self._run(
            [
                *self._base_command(),
                "--open-session",
                record.session_id,
                "--open-session-record-stdin",
            ],
            timeout=30,
            input_text=json.dumps(
                record.to_json(include_search_snippet=False),
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _run(
        command: Sequence[str],
        *,
        timeout: float,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                input=input_text,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PickerError(f"could not run session bridge: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            if len(detail) > 500:
                detail = detail[-500:]
            raise PickerError(detail or f"session bridge exited {completed.returncode}")
        return completed


CatalogResult: TypeAlias = tuple[int, str, list[SessionRecord] | None, str | None]


class CatalogWorker:
    """One coalescing daemon worker for debounced, blocking HTTP discovery."""

    def __init__(
        self,
        catalog: Callable[[str], list[SessionRecord]],
        deliver: Callable[[CatalogResult], None],
    ) -> None:
        self._catalog = catalog
        self._deliver = deliver
        self._condition = threading.Condition()
        self._request: tuple[int, str, float] | None = None
        self._generation = 0
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request(self, query: str, delay: float) -> int:
        normalized_query = _normalize_query(query)
        with self._condition:
            self._generation += 1
            generation = self._generation
            self._request = (generation, normalized_query, time.monotonic() + delay)
            self._condition.notify()
            return generation

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._request is None and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                request = self._request
                assert request is not None
                generation, query, ready_at = request
                remaining = ready_at - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                self._request = None
            try:
                records = self._catalog(query)
            except PickerError as exc:
                self._deliver((generation, query, None, str(exc)))
            else:
                self._deliver((generation, query, records, None))


def _truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


def _relative_age(updated_at: int | None, *, now: float | None = None) -> str:
    if updated_at is None:
        return "—"
    delta = max(0, int((time.time() if now is None else now) - updated_at))
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86_400:
        return f"{delta // 3600}h"
    if delta < 604_800:
        return f"{delta // 86_400}d"
    return f"{delta // 604_800}w"


def _status_glyph(record: SessionRecord) -> str:
    if record.attention:
        return "!"
    return {"running": "●", "idle": "○", "waiting": "!", "failed": "×"}[record.status]


def _row_text(record: SessionRecord, *, selected: bool, width: int) -> str:
    pointer = "›" if selected else " "
    pin = "★" if record.pinned else " "
    materialized = "OPEN" if record.loaded else "LOAD"
    project = _truncate(record.project or "Unfiled", 18)
    agent = _truncate(record.agent or "Omnigent", 10)
    trailing = f"  {project:<18}  {agent:<10}  {_relative_age(record.updated_at):>4}"
    prefix = f"{pointer} {pin} {_status_glyph(record)} {materialized:<4}  "
    title_width = max(8, width - len(prefix) - len(trailing) - 1)
    line = f"{prefix}{_truncate(record.label, title_width):<{title_width}}{trailing}"
    return _truncate(line, max(1, width)).ljust(max(1, width))


def _agent_row_text(agent: AgentRecord, *, selected: bool, width: int) -> str:
    pointer = "›" if selected else " "
    harness = agent.harness or "harness unavailable"
    trailing = f"  {harness}"
    prefix = f"{pointer}  "
    name_width = max(8, width - len(prefix) - len(trailing) - 1)
    line = f"{prefix}{_truncate(agent.display_name, name_width):<{name_width}}{trailing}"
    return _truncate(line, max(1, width)).ljust(max(1, width))


def _style() -> Any:
    from prompt_toolkit.styles import Style

    return Style.from_dict(
        {
            "brand": "bold #f43ba6",
            "muted": "#6a6a6a",
            "rule": "#3a3a3a",
            "scope": "#8a8a8a",
            "scope.active": "bold #f43ba6",
            "query": "#f4f4f4",
            "row": "#bcbcbc",
            "row.selected": "bold #ffffff bg:#4a1640",
            "detail": "#9a9a9a",
            "error": "bold #ff5f5f",
            "busy": "#f43ba6",
            "footer": "#777777",
        }
    )


def run_picker(
    bridge: SessionSpaceBridge,
    config: PickerConfig,
    *,
    input_stream: Any | None = None,
    output_stream: Any | None = None,
) -> str | None:
    from prompt_toolkit.application import Application, get_app
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.processors import BeforeInput
    from prompt_toolkit.layout.screen import Point
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

    cache = CatalogCache()
    persistent_cache = PersistentCatalogCache(
        config.catalog_cache_file,
        scope=config.catalog_cache_scope,
    )
    persistent_agent_cache = PersistentAgentCache(
        config.agent_cache_file,
        scope=config.catalog_cache_scope,
    )
    state = PickerState(loading=True)
    new_session = NewSessionState()
    local_created_records: dict[str, SessionRecord] = {}
    local_created_records_lock = threading.RLock()
    cached_records = persistent_cache.load()
    cached_agents = persistent_agent_cache.load()
    if cached_records is not None:
        cache.store("", cached_records)
        state.set_records(cached_records, preserve_selection=False)
        state.displayed_query = ""
    application: Application[str | None] | None = None
    worker: CatalogWorker | None = None

    def invalidate() -> None:
        if application is not None:
            application.invalidate()

    def call_on_application_loop(callback: Callable[[], None]) -> None:
        if application is None or application.loop is None or application.loop.is_closed():
            return
        application.loop.call_soon_threadsafe(callback)

    def upsert_created_record(record: SessionRecord) -> None:
        with local_created_records_lock:
            local_created_records[record.session_id] = record
            cache.upsert(record)
            base_records = cache.base_records()
            if base_records is not None:
                persistent_cache.store(base_records)

    def merge_created_records(
        records: Sequence[SessionRecord],
        *,
        query: str,
    ) -> list[SessionRecord]:
        with local_created_records_lock:
            for record in records:
                local = local_created_records.get(record.session_id)
                if local is not None:
                    local_created_records[record.session_id] = replace(
                        record,
                        loaded=record.loaded or local.loaded,
                    )
            created_records = tuple(local_created_records.values())
        return _merge_created_records(records, created_records, query=query)

    def apply_catalog(result: CatalogResult) -> None:
        generation, query, records, error = result
        if records is not None:
            records = merge_created_records(records, query=query)
            cache.store(query, records)
        if generation != state.request_generation:
            return
        state.loading = False
        state.error = error
        if records is not None:
            state.set_records(
                records,
                preserve_selection=state.displayed_query == query,
            )
            state.displayed_query = query
        invalidate()

    def deliver(result: CatalogResult) -> None:
        generation, query, records, error = result
        if records is not None:
            with local_created_records_lock:
                records = merge_created_records(records, query=query)
                if query == "":
                    persistent_cache.store(records)
            result = (generation, query, records, error)
        if application is not None and application.loop is not None:
            if not application.loop.is_closed():
                application.loop.call_soon_threadsafe(apply_catalog, result)

    worker = CatalogWorker(bridge.catalog, deliver)

    def request_catalog(*, immediate: bool = False) -> None:
        assert worker is not None
        state.loading = True
        state.error = None
        state.request_generation = worker.request(
            _normalize_query(state.query),
            0 if immediate else config.debounce_seconds,
        )
        invalidate()

    def query_changed(buffer: Buffer) -> None:
        if new_session.active:
            new_session.set_query(buffer.text)
            invalidate()
            return
        state.query = buffer.text
        state.selected_index = 0
        normalized_query = _normalize_query(state.query)
        cached = cache.lookup(normalized_query)
        if cached is not None and (
            cached or cache.contains(normalized_query) or not state.records
        ):
            state.set_records(cached, preserve_selection=False)
            state.displayed_query = normalized_query
        request_catalog()

    def prompt_changed(buffer: Buffer) -> None:
        new_session.set_prompt(buffer.text)
        invalidate()

    search_buffer = Buffer(
        multiline=False,
        on_text_changed=query_changed,
        read_only=Condition(lambda: new_session.busy),
    )
    prompt_buffer = Buffer(
        multiline=True,
        on_text_changed=prompt_changed,
        read_only=Condition(lambda: new_session.phase != "compose_message"),
    )

    def set_prompt_buffer(value: str) -> None:
        prompt_buffer.set_document(
            Document(value, cursor_position=len(value)),
            bypass_readonly=True,
        )

    def prompt_visible() -> bool:
        return new_session.phase in {
            "compose_message",
            "creating",
            "sending",
            "send_unknown",
            "opening",
            "create_failed",
            "open_failed",
        }

    def search_prefix() -> StyleAndTextTuples:
        label = "agent " if new_session.active else ""
        return [("class:brand", f"{label}❯ ")]

    search_control = BufferControl(
        buffer=search_buffer,
        input_processors=[BeforeInput(search_prefix)],
        focusable=True,
    )

    prompt_control = BufferControl(
        buffer=prompt_buffer,
        input_processors=[BeforeInput(lambda: [("class:brand", "message ❯ ")])],
        focusable=True,
    )

    def header_fragments() -> StyleAndTextTuples:
        if new_session.active:
            return [
                ("class:brand", " OMNIGENT "),
                ("", " NEW SESSION"),
                ("class:muted", f"   {len(new_session.agents)} AGENTS"),
            ]
        return [
            ("class:brand", " OMNIGENT "),
            ("", " SESSION SWITCHER"),
            ("class:muted", f"   {len(state.records)} CATALOGED"),
        ]

    def scope_fragments() -> StyleAndTextTuples:
        if new_session.active:
            if prompt_visible():
                agent_name = (
                    new_session.chosen_agent.display_name
                    if new_session.chosen_agent is not None
                    else "agent unavailable"
                )
                return [
                    ("class:scope.active", "  INITIAL MESSAGE  "),
                    ("class:muted", f"  {agent_name}  ·  {config.fallback_cwd}"),
                ]
            return [
                ("class:scope.active", "  CHOOSE AGENT  "),
                ("class:muted", f"  {config.fallback_cwd}"),
            ]
        fragments: StyleAndTextTuples = []
        for scope in SCOPES:
            style = "class:scope.active" if state.scope == scope else "class:scope"
            fragments.append((style, f"  {scope.upper()} {state.count(scope)}  "))
        return fragments

    def mouse_handler(index: int) -> Callable[[MouseEvent], None]:
        def handle(event: MouseEvent) -> None:
            if event.event_type in {MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP}:
                if new_session.active:
                    if not new_session.busy:
                        new_session.selected_index = index
                else:
                    state.selected_index = index
                invalidate()

        return handle

    def row_fragments() -> StyleAndTextTuples:
        if new_session.active:
            if prompt_visible():
                agent_name = (
                    new_session.chosen_agent.display_name
                    if new_session.chosen_agent is not None
                    else "Omnigent"
                )
                return [
                    (
                        "class:detail",
                        f"  New {agent_name} session\n  The first message starts the turn.",
                    )
                ]
            visible_agents = new_session.visible_agents
            if not visible_agents:
                if new_session.phase == "loading_agents":
                    message = "  Loading agents…"
                elif new_session.phase == "agent_error":
                    message = "  Agent catalog unavailable"
                else:
                    message = "  No agents match this filter"
                return [("class:muted", message)]
            width = max(32, get_app().output.get_size().columns - 2)
            fragments: StyleAndTextTuples = []
            for index, agent in enumerate(visible_agents):
                selected = index == new_session.selected_index
                fragments.append(
                    (
                        "class:row.selected" if selected else "class:row",
                        _agent_row_text(agent, selected=selected, width=width),
                        mouse_handler(index),
                    )
                )
                if index != len(visible_agents) - 1:
                    fragments.append(("", "\n"))
            return fragments
        visible = state.visible_records
        if not visible:
            if state.loading and state.displayed_query != _normalize_query(state.query):
                message = "  Searching…"
            elif state.loading:
                message = "  No cached matches · searching conversation content…"
            else:
                message = "  No sessions in this scope"
            return [("class:muted", message)]
        width = max(32, get_app().output.get_size().columns - 2)
        fragments: StyleAndTextTuples = []
        for index, record in enumerate(visible):
            selected = index == state.selected_index
            fragments.append(
                (
                    "class:row.selected" if selected else "class:row",
                    _row_text(record, selected=selected, width=width),
                    mouse_handler(index),
                )
            )
            if index != len(visible) - 1:
                fragments.append(("", "\n"))
        return fragments

    def cursor_position() -> Point:
        if prompt_visible():
            return Point(x=0, y=0)
        selected_index = new_session.selected_index if new_session.active else state.selected_index
        return Point(x=0, y=selected_index)

    def detail_fragments() -> StyleAndTextTuples:
        if new_session.active:
            if prompt_visible():
                return [
                    (
                        "class:detail",
                        "  Enter starts · Ctrl-J inserts a newline\n"
                        "  The first nonblank line is the temporary Space name",
                    )
                ]
            selected_agent = new_session.selected_agent
            if selected_agent is None:
                agent_detail = "Choose the agent for the new session"
            else:
                harness = selected_agent.harness or "harness unavailable"
                agent_detail = f"{selected_agent.display_name}  ·  {harness}"
            return [
                (
                    "class:detail",
                    f"  {agent_detail}\n  Start in {config.fallback_cwd}",
                )
            ]
        selected = state.selected
        if selected is None:
            return [("class:detail", "  Type to search titles and conversation content")]
        workspace = selected.workspace or "workspace unavailable"
        snippet = f"  ·  {selected.search_snippet}" if selected.search_snippet else ""
        return [
            (
                "class:detail",
                f"  {selected.recovery_hint.upper()}  ·  {selected.status.upper()}"
                f"  ·  {workspace}{snippet}\n  {selected.session_id}",
            )
        ]

    def status_fragments() -> StyleAndTextTuples:
        if new_session.active:
            selected_agent = new_session.chosen_agent or new_session.selected_agent
            if new_session.phase == "loading_agents":
                return [("class:busy", "  Loading available agents…")]
            if new_session.agent_loading and new_session.agents:
                return [
                    (
                        "class:muted",
                        f"  {len(new_session.visible_agents)} cached agents · refreshing…",
                    )
                ]
            if new_session.phase == "creating" and selected_agent is not None:
                return [
                    (
                        "class:busy",
                        f"  Creating {selected_agent.display_name} session…",
                    )
                ]
            if new_session.phase == "sending" and new_session.created_record is not None:
                return [
                    (
                        "class:busy",
                        f"  Created {new_session.created_record.session_id} · "
                        "sending first message…",
                    )
                ]
            if new_session.phase == "opening" and new_session.created_record is not None:
                return [
                    (
                        "class:busy",
                        f"  Created {new_session.created_record.session_id} · opening workspace…",
                    )
                ]
            if new_session.phase == "send_unknown":
                return [
                    (
                        "class:error",
                        "  Initial-message outcome is unknown; it will not be resent · "
                        f"Enter opens for inspection: {new_session.error or 'unknown error'}",
                    )
                ]
            if new_session.phase == "open_failed":
                return [
                    (
                        "class:error",
                        "  Session created, but opening failed · Enter retries open only: "
                        f"{new_session.error or 'unknown error'}",
                    )
                ]
            if new_session.phase == "create_failed":
                return [
                    (
                        "class:error",
                        "  Create failed or its outcome is unknown · Esc, then refresh before "
                        f"trying again: {new_session.error or 'unknown error'}",
                    )
                ]
            if new_session.phase == "agent_error":
                return [
                    (
                        "class:error",
                        f"  Could not load agents · Ctrl-R retries: {new_session.error}",
                    )
                ]
            if new_session.error is not None and new_session.phase == "choose_agent":
                return [
                    (
                        "class:error",
                        f"  Showing cached agents · Ctrl-R retries: {new_session.error}",
                    )
                ]
            if new_session.phase == "compose_message":
                if not new_session.prompt.strip():
                    return [("class:muted", "  An initial message is required")]
                agent_label = (
                    selected_agent.display_name if selected_agent is not None else "selected agent"
                )
                return [
                    (
                        "class:muted",
                        f"  Ready to create once as {agent_label}",
                    )
                ]
            return [
                (
                    "class:muted",
                    f"  {len(new_session.visible_agents)} agents shown · Enter creates once",
                )
            ]
        if state.opening and state.selected is not None:
            return [("class:busy", f"  Opening {state.selected.label}…")]
        if state.error:
            return [("class:error", f"  {state.error}")]
        if state.loading:
            cache_kind = (
                "cached" if state.displayed_query == _normalize_query(state.query) else "previous"
            )
            return [
                (
                    "class:muted",
                    f"  {len(state.visible_records)} {cache_kind} · refreshing Omnigent…",
                )
            ]
        return [
            (
                "class:muted",
                f"  {len(state.visible_records)} shown  ·  "
                f"{sum(record.loaded for record in state.records)} open",
            )
        ]

    def footer_fragments() -> StyleAndTextTuples:
        if new_session.busy:
            return [("class:footer", "  Creating/sending/opening… please wait")]
        if new_session.phase == "send_unknown":
            return [
                (
                    "class:footer",
                    "  enter open without resending   esc sessions   ^C close",
                )
            ]
        if new_session.phase == "open_failed":
            return [
                (
                    "class:footer",
                    "  enter retry open   esc sessions   ^C close",
                )
            ]
        if new_session.phase == "create_failed":
            return [
                (
                    "class:footer",
                    "  esc sessions   ^C close   refresh before another create",
                )
            ]
        if new_session.active:
            if new_session.phase == "compose_message":
                return [
                    (
                        "class:footer",
                        "  enter create + send   ^J newline   esc agents   ^C close",
                    )
                ]
            return [
                (
                    "class:footer",
                    "  ↑↓/^P move   type filter   enter message   ^R agents   esc back   ^C close",
                )
            ]
        return [
            (
                "class:footer",
                "  ↑↓/^P move   ^N new   tab scope   enter open   ^R refresh   esc close",
            )
        ]

    key_bindings = KeyBindings()
    navigation_enabled = Condition(lambda: new_session.phase != "compose_message")

    def finish_agent_load(
        generation: int,
        agents: Sequence[AgentRecord] | None,
        error: str | None,
    ) -> None:
        nonlocal cached_agents
        if agents is not None and error is None:
            cached_agents = list(agents)
        if new_session.finish_agent_load(generation, agents, error):
            invalidate()

    def load_create_agents() -> None:
        generation = new_session.start_agent_load()
        if generation is None:
            return
        request_generation = generation
        invalidate()

        def run() -> None:
            agents: list[AgentRecord] | None = None
            error: str | None = None
            try:
                agents = bridge.list_create_agents()
            except PickerError as exc:
                error = str(exc)
            if agents is not None:
                persistent_agent_cache.store(agents)
            call_on_application_loop(lambda: finish_agent_load(request_generation, agents, error))

        threading.Thread(target=run, daemon=True).start()

    def enter_new_session() -> None:
        selected_id = state.selected.session_id if state.selected is not None else None
        if state.opening or not new_session.enter(state.query, selected_id):
            return
        search_buffer.text = ""
        set_prompt_buffer("")
        if cached_agents is not None:
            new_session.seed_agents(cached_agents)
        load_create_agents()

    def leave_new_session() -> None:
        restore = new_session.leave()
        if restore is None:
            return
        browse_query, selected_id = restore
        set_prompt_buffer("")
        search_buffer.text = browse_query
        state.query = browse_query
        normalized_query = _normalize_query(browse_query)
        cached = cache.lookup(normalized_query)
        if cached is not None:
            state.set_records(cached, preserve_selection=False)
            state.displayed_query = normalized_query
        state.select_session(selected_id)
        invalidate()

    def start_message_composer() -> None:
        if new_session.begin_compose() is None:
            return
        set_prompt_buffer(new_session.prompt)
        if application is not None:
            application.layout.focus(prompt_control)
        invalidate()

    def return_to_agent_choice() -> None:
        if not new_session.return_to_agent_choice():
            return
        if application is not None:
            application.layout.focus(search_control)
        invalidate()

    def finish_created_open(record: SessionRecord, error: str | None) -> None:
        if error is not None:
            new_session.mark_open_failed(error)
            invalidate()
            return
        materialized = replace(record, loaded=True)
        new_session.created_record = materialized
        upsert_created_record(materialized)
        if application is not None and application.is_running:
            application.exit(result=record.session_id)

    def open_created(record: SessionRecord) -> None:
        def run() -> None:
            error: str | None = None
            try:
                bridge.open(record)
            except PickerError as exc:
                error = str(exc)
            call_on_application_loop(lambda: finish_created_open(record, error))

        threading.Thread(target=run, daemon=True).start()

    def finish_create(record: SessionRecord | None, error: str | None) -> None:
        if error is not None:
            new_session.mark_create_failed(error)
            invalidate()
            return
        assert record is not None
        record = replace(record, title=_initial_prompt_label(new_session.prompt))
        if not new_session.mark_created(record):
            return
        upsert_created_record(record)
        invalidate()
        send_initial_message(record)

    def finish_initial_message(error: str | None) -> None:
        if error is not None:
            new_session.mark_send_unknown(error)
            invalidate()
            return
        open_record = new_session.mark_message_sent()
        if open_record is None:
            return
        invalidate()
        open_created(open_record)

    def send_initial_message(record: SessionRecord) -> None:
        prompt = new_session.prompt

        def run() -> None:
            error: str | None = None
            try:
                bridge.send_initial_message(record.session_id, prompt)
            except PickerError as exc:
                error = str(exc)
            call_on_application_loop(lambda: finish_initial_message(error))

        threading.Thread(target=run, daemon=True).start()

    def create_selected_agent() -> None:
        submission = new_session.begin_create()
        if submission is None:
            return
        agent, _prompt = submission
        invalidate()

        def run() -> None:
            record: SessionRecord | None = None
            error: str | None = None
            try:
                record = bridge.create(agent.agent_id)
            except PickerError as exc:
                error = str(exc)
            call_on_application_loop(lambda: finish_create(record, error))

        threading.Thread(target=run, daemon=True).start()

    def retry_created_open() -> None:
        record = new_session.begin_open_retry()
        if record is None:
            return
        invalidate()
        open_created(record)

    def open_after_unknown_send() -> None:
        record = new_session.begin_open_after_unknown_send()
        if record is None:
            return
        invalidate()
        open_created(record)

    @key_bindings.add("up", filter=navigation_enabled, eager=True)
    @key_bindings.add("c-p", filter=navigation_enabled, eager=True)
    def move_up(event: Any) -> None:
        if new_session.active:
            if new_session.phase == "choose_agent":
                new_session.move(-1)
        else:
            state.move(-1)
        event.app.invalidate()

    @key_bindings.add("down", filter=navigation_enabled, eager=True)
    def move_down(event: Any) -> None:
        if new_session.active:
            if new_session.phase == "choose_agent":
                new_session.move(1)
        else:
            state.move(1)
        event.app.invalidate()

    @key_bindings.add("c-n", filter=navigation_enabled, eager=True)
    def new_session_picker(event: Any) -> None:
        enter_new_session()
        event.app.invalidate()

    @key_bindings.add("pageup", filter=navigation_enabled, eager=True)
    def page_up(event: Any) -> None:
        if new_session.active:
            if new_session.phase == "choose_agent":
                new_session.move(-10)
        else:
            state.move(-10)
        event.app.invalidate()

    @key_bindings.add("pagedown", filter=navigation_enabled, eager=True)
    def page_down(event: Any) -> None:
        if new_session.active:
            if new_session.phase == "choose_agent":
                new_session.move(10)
        else:
            state.move(10)
        event.app.invalidate()

    @key_bindings.add("tab", filter=navigation_enabled, eager=True)
    def next_scope(event: Any) -> None:
        if not new_session.active:
            state.cycle_scope()
        event.app.invalidate()

    @key_bindings.add("s-tab", filter=navigation_enabled, eager=True)
    def previous_scope(event: Any) -> None:
        if not new_session.active:
            state.cycle_scope(-1)
        event.app.invalidate()

    @key_bindings.add("c-r", filter=navigation_enabled, eager=True)
    def refresh(event: Any) -> None:
        if new_session.phase in {"loading_agents", "choose_agent", "agent_error"}:
            load_create_agents()
        elif not new_session.active:
            request_catalog(immediate=True)
        event.app.invalidate()

    def finish_open(record: SessionRecord, error: str | None) -> None:
        state.opening = False
        if error is not None:
            state.error = error
            invalidate()
            return
        if application is not None and application.is_running:
            application.exit(result=record.session_id)

    def open_selected() -> None:
        if new_session.active:
            return
        record = state.selected
        if record is None or not state.can_open:
            return
        state.opening = True
        state.error = None
        invalidate()

        def run() -> None:
            error: str | None = None
            try:
                bridge.open(record)
            except PickerError as exc:
                error = str(exc)
            call_on_application_loop(lambda: finish_open(record, error))

        threading.Thread(target=run, daemon=True).start()

    @key_bindings.add("enter", eager=True)
    def select(_event: Any) -> None:
        if new_session.phase == "choose_agent":
            start_message_composer()
        elif new_session.phase == "compose_message":
            create_selected_agent()
        elif new_session.phase == "send_unknown":
            open_after_unknown_send()
        elif new_session.phase == "open_failed":
            retry_created_open()
        elif not new_session.active:
            open_selected()

    @key_bindings.add("c-j", eager=True)
    def insert_prompt_newline(event: Any) -> None:
        if new_session.phase == "compose_message":
            event.current_buffer.insert_text("\n")

    @key_bindings.add("escape", eager=True)
    def escape(event: Any) -> None:
        if new_session.phase == "compose_message":
            return_to_agent_choice()
        elif new_session.active:
            if not new_session.busy:
                leave_new_session()
        elif not state.opening:
            event.app.exit(result=None)

    @key_bindings.add("c-c", eager=True)
    def cancel(event: Any) -> None:
        if not state.opening and not new_session.busy:
            event.app.exit(result=None)

    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(header_fragments), height=1),
                ConditionalContainer(
                    Window(content=search_control, height=1, style="class:query"),
                    filter=Condition(lambda: not prompt_visible()),
                ),
                ConditionalContainer(
                    Window(
                        content=prompt_control,
                        height=Dimension(min=3, max=7, preferred=4),
                        style="class:query",
                        wrap_lines=True,
                    ),
                    filter=Condition(prompt_visible),
                ),
                Window(FormattedTextControl(scope_fragments), height=1),
                Window(char="─", height=1, style="class:rule"),
                Window(
                    FormattedTextControl(
                        row_fragments,
                        get_cursor_position=cursor_position,
                    ),
                    height=Dimension(min=3),
                    wrap_lines=False,
                    always_hide_cursor=True,
                ),
                Window(char="─", height=1, style="class:rule"),
                Window(FormattedTextControl(detail_fragments), height=2, wrap_lines=False),
                Window(FormattedTextControl(status_fragments), height=1, wrap_lines=False),
                Window(
                    FormattedTextControl(footer_fragments),
                    height=1,
                ),
            ]
        ),
        focused_element=search_control,
    )
    application = Application[str | None](
        layout=layout,
        key_bindings=key_bindings,
        style=_style(),
        full_screen=True,
        mouse_support=True,
        include_default_pygments_style=False,
        min_redraw_interval=0.03,
        input=input_stream,
        output=output_stream,
    )
    try:
        return application.run(
            pre_run=lambda: request_catalog(immediate=True),
            handle_sigint=False,
            set_exception_handler=False,
        )
    finally:
        worker.stop()


def main() -> int:
    if sys.version_info < (3, 12):
        print(
            "Omnigent session picker requires Python 3.12+; launch it through uv ",
            "or the Omnigent virtual environment.",
            file=sys.stderr,
        )
        return 2
    try:
        config = PickerConfig.load()
        run_picker(SessionSpaceBridge(config), config)
    except PickerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
