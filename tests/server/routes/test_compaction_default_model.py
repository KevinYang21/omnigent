"""Unit tests for the model-less-spec compaction default resolution.

Regression: server-side ``/compact`` rejected any spec pinning no model
(neither ``llm.model`` nor ``executor.model``) with "does not declare an LLM
model", even though turn-time execution happily resolves the harness's
provider-family default for the same spec. ``_default_compaction_llm_config``
closes that gap: family catalog default first, then the server-level ``llm:``
config, else ``None`` (the caller raises the actionable error).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._sessions.helpers import _default_compaction_llm_config
from omnigent.spec.types import LLMConfig


def _spec(harness_kind: str, connection: dict[str, str] | None = None) -> SimpleNamespace:
    """Build a minimal spec shape for a model-less agent."""
    return SimpleNamespace(
        executor=SimpleNamespace(harness_kind=harness_kind, connection=connection)
    )


def _patch_catalog(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """Pin the family→catalog-default resolution to *mapping*."""

    def _resolve(family: str) -> str:
        try:
            return mapping[family]
        except KeyError:
            raise OmnigentError("no catalog default", code=ErrorCode.INVALID_INPUT) from None

    monkeypatch.setattr("omnigent.runtime.workflow._catalog_default_model", _resolve)


def _patch_server_llm(monkeypatch: pytest.MonkeyPatch, llm: LLMConfig | None) -> None:
    """Pin the server-level ``llm:`` config the fallback tier reads."""
    monkeypatch.setattr("omnigent.runtime.get_caps", lambda: SimpleNamespace(llm=llm))


def test_family_default_resolves_for_openai_sdk_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An openai-family harness gets the openai catalog default, carrying
    the executor's connection overrides."""
    _patch_catalog(monkeypatch, {"openai": "gpt-default"})
    _patch_server_llm(monkeypatch, None)

    cfg = _default_compaction_llm_config(_spec("openai-agents", {"api_key": "k"}))

    assert cfg is not None
    assert cfg.model == "gpt-default"
    assert cfg.connection == {"api_key": "k"}


def test_executor_type_spelling_maps_to_family(monkeypatch: pytest.MonkeyPatch) -> None:
    """``harness_kind`` may be the executor-type spelling (``agents_sdk``);
    it must map to the same openai family."""
    _patch_catalog(monkeypatch, {"openai": "gpt-default"})
    _patch_server_llm(monkeypatch, None)

    cfg = _default_compaction_llm_config(_spec("agents_sdk"))

    assert cfg is not None
    assert cfg.model == "gpt-default"


def test_server_llm_fallback_when_catalog_has_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No catalog default (discovery disabled/unreachable) → the server's
    own ``llm:`` config is used."""
    _patch_catalog(monkeypatch, {})
    server_llm = LLMConfig(model="server-model")
    _patch_server_llm(monkeypatch, server_llm)

    cfg = _default_compaction_llm_config(_spec("openai-agents"))

    assert cfg is server_llm


def test_server_llm_fallback_for_familyless_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness with no single provider family (e.g. goose) skips the
    catalog tier but still uses the server ``llm:``."""
    _patch_catalog(monkeypatch, {"openai": "gpt-default"})
    server_llm = LLMConfig(model="server-model")
    _patch_server_llm(monkeypatch, server_llm)

    cfg = _default_compaction_llm_config(_spec("goose"))

    assert cfg is server_llm


def test_none_when_no_tier_yields_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """No family default and no server ``llm:`` → ``None`` so the caller
    raises the actionable unavailable error."""
    _patch_catalog(monkeypatch, {})
    _patch_server_llm(monkeypatch, None)

    assert _default_compaction_llm_config(_spec("openai-agents")) is None


def test_none_when_server_llm_model_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server ``llm:`` whose model is empty must not be treated as usable."""
    _patch_catalog(monkeypatch, {})
    _patch_server_llm(monkeypatch, LLMConfig(model=""))

    assert _default_compaction_llm_config(_spec("openai-agents")) is None
