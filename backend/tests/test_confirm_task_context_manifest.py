"""Regression guard for the section-confirmation context update."""


def test_section_confirmation_keeps_the_frozen_context_manifest():
    from app.api.v1.agent_tasks import merge_section_confirm_config

    manifest = {
        "engine": "langgraph",
        "task_semantic_flags": {
            "CONTEXT_ENGINE_AGENT_ENABLED": True,
            "MIG_GENERATE": True,
        },
    }
    original = {
        "frozen_flags_manifest": manifest,
        "knowledge_mode": "project",
    }

    merged = merge_section_confirm_config(original, [{"section_id": "body_1"}])

    assert merged["frozen_flags_manifest"] == manifest
    assert merged["knowledge_mode"] == "project"
    assert merged["section_confirm_config"] == {"sections": [{"section_id": "body_1"}]}


def test_retry_reuses_a_valid_frozen_manifest():
    from app.services.agent_task_service import build_retry_task_context

    manifest = {
        "engine": "langgraph",
        "task_semantic_flags": {"CONTEXT_ENGINE_AGENT_ENABLED": True},
    }
    source = {"frozen_flags_manifest": manifest, "section_confirm_config": {}}

    retried = build_retry_task_context(
        source,
        engine_type="langgraph",
        graph_version="v3",
    )

    assert retried == source


def test_retry_rebuilds_a_manifest_when_the_source_was_corrupted(monkeypatch):
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from app.services.agent_task_service import build_retry_task_context

    monkeypatch.setattr(
        "app.context_engine.feature_flags.get_context_engine_flags",
        lambda: ContextEngineFeatureFlags(
            context_engine_agent_enabled=True,
            mig_generate=True,
        ),
    )

    retried = build_retry_task_context(
        {"section_confirm_config": {"sections": []}},
        engine_type="langgraph",
        graph_version="v3",
    )

    manifest = retried["frozen_flags_manifest"]
    assert manifest["engine_decision_reason"] == "context_flags.freeze_at_retry"
    assert manifest["task_semantic_flags"]["CONTEXT_ENGINE_AGENT_ENABLED"] is True
    assert manifest["task_semantic_flags"]["MIG_GENERATE"] is True
