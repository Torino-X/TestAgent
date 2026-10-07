"""CE-05 WP-13 灰度发布准备测试。

覆盖：
  - retired Legacy route flag is absent
  - MIG flags 默认 False
  - .env.example 含全部 CE-05 env 模板
  - new task engine is fixed to LangGraph
  - 发布检查点文档存在（READY FOR HUMAN EXECUTION）
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_legacy_orchestrator_flag_is_retired():
    from app.context_engine.feature_flags import ContextFeatureFlag

    assert "LEGACY_ORCHESTRATOR_ENABLED" not in ContextFeatureFlag.__members__


def test_mig_flags_default_false():
    from app.context_engine.feature_flags import ContextEngineFeatureFlags

    flags = ContextEngineFeatureFlags()
    for mig in ("mig_review", "mig_repair", "mig_generate", "mig_preparation",
                "mig_incremental", "mig_chat", "mig_summary", "mig_narrative"):
        assert getattr(flags, mig) is False, f"{mig} 应默认 False"


def test_env_example_has_ce05_templates():
    env_path = _repo_root() / "backend" / ".env.example"
    text = env_path.read_text(encoding="utf-8")
    assert "LEGACY_ORCHESTRATOR_ENABLED" not in text
    for marker in ("MIG_REVIEW", "MIG_CHAT",
                   "CONTEXT_PII_MODE", "CONTEXT_CURSOR_SECRET",
                   "CONTEXT_RETENTION_DRY_RUN", "CONTEXT_DEBUG_API_ENABLED",
                   "CONTEXT_FULL_PROMPT_DEBUG_ENABLED"):
        assert marker in text, f".env.example 缺 {marker}"


def test_new_task_engine_is_fixed_to_langgraph():
    service = (_repo_root() / "backend" / "app" / "services" / "message_service.py")
    text = service.read_text(encoding="utf-8")
    assert "EngineRouter" not in text
    assert 'engine_type = "langgraph"' in text


def test_release_checkpoint_doc_exists():
    """发布检查点文档存在且标记 READY FOR HUMAN EXECUTION。"""
    doc = _repo_root() / "docs" / "context-engine" / "CE" / "CE-05_WP13_发布准备检查点.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "READY FOR HUMAN EXECUTION" in text
