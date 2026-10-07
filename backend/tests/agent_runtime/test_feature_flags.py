"""LangGraph runtime feature flags after engine-routing retirement."""

from __future__ import annotations

from unittest.mock import patch

from app.agent_runtime.feature_flags import AgentRuntimeFeatureFlags, get_feature_flags


def test_engine_compatibility_fields_cannot_select_legacy() -> None:
    flags = AgentRuntimeFeatureFlags()
    assert not hasattr(flags, "default_engine")
    assert not hasattr(flags, "allow_user_override")
    assert not hasattr(flags, "resolve_engine")


def test_production_dispatch_requires_explicit_env() -> None:
    with patch.dict("os.environ", {}, clear=True):
        flags = get_feature_flags()
    assert flags.langgraph_enabled is False
    assert flags.production_dispatch_enabled is False
    assert not hasattr(flags, "default_engine")


def test_langgraph_and_production_dispatch_can_be_enabled() -> None:
    with patch.dict(
        "os.environ",
        {
            "AGENT_RUNTIME_LANGGRAPH_ENABLED": "1",
            "AGENT_RUNTIME_PRODUCTION_DISPATCH_ENABLED": "1",
        },
        clear=True,
    ):
        flags = get_feature_flags()
    assert flags.langgraph_enabled is True
    assert flags.production_dispatch_enabled is True
