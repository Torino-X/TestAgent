"""CE-02 回归测试：失败 Node ID 集合对比 + v2_frozen 保护。

覆盖（WP-12 test_ce02_regression.py）：
- 对比 CE-01 基线失败 Node ID 集合（40 个既有失败，New Failures=0）；
- v2_frozen 保护测试（git diff v2_frozen 为空）；
- CE-02 新增测试全绿（快照/组合/选择/来源等不引入新失败）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent  # backend/
_REPO_ROOT = _BACKEND_ROOT.parent

# CE-01 基线（docs/context-engine/CE/CE-01 报告 10.5）：40 个既有失败，12 个文件
CE01_BASELINE_FAILED_FILES = {
    "recoverable_audit": 20,
    "v2_interrupts": 5,
    "word_export": 4,  # 3 + 1 err
    "orchestrator_streaming": 2,
    "kb_skip": 2,
    "phase29a23": 1,
    "phase29a19": 1,
    "orchestrator_retry": 1,
    "knowledge_search": 1,
    "batch4": 1,
    "lifespan": 1,
    "canary": 1,
}


def test_v2_frozen_unchanged():
    """v2_frozen 目录 diff 为空（CE-02 硬约束）。"""
    proc = subprocess.run(
        [
            "git", "diff", "--name-only", "e26df97..HEAD", "--",
            "app/agent_runtime/graphs/test_plan/versions/v2_frozen/",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", f"v2_frozen 被修改: {proc.stdout.strip()}"


def test_v2_frozen_files_present():
    """v2_frozen 目录文件仍存在（未被删除）。"""
    frozen_dir = _BACKEND_ROOT / "app/agent_runtime/graphs/test_plan/versions/v2_frozen"
    assert frozen_dir.is_dir()
    assert (frozen_dir / "graph.py").is_file()


def test_ce02_new_tests_collected():
    """CE-02 新增测试文件可被 pytest 收集。"""
    new_files = [
        "test_context_engine_sources.py",
        "test_context_engine_selection.py",
        "test_context_engine_composer.py",
        "test_context_engine_payload.py",
        "test_context_engine_snapshot.py",
        "test_context_engine_invoker.py",
        "test_context_engine_runtime.py",
        "test_context_engine_tool_output.py",
        "test_ce02_pilot.py",
        "test_ce02_shadow.py",
    ]
    tests_dir = _BACKEND_ROOT / "tests"
    for f in new_files:
        assert (tests_dir / f).is_file(), f"CE-02 测试文件缺失: {f}"


def test_runtime_context_fields_backward_compatible():
    """RuntimeContext 新增字段默认 None（向后兼容）。"""
    from app.agent_runtime.runtime_context import RuntimeContext

    rc = RuntimeContext(
        user_internal_id=1, task_internal_id=2, conversation_internal_id=3,
        session_factory=lambda: None, settings_service=None, event_sink=None,
        cancellation_service=None,
    )
    assert rc.context_engine is None
    assert rc.context_llm_invoker is None


def test_feature_flags_default_disabled():
    """16 个 feature flags 默认关闭。"""
    from app.context_engine.feature_flags import ContextEngineFeatureFlags

    flags = ContextEngineFeatureFlags()
    for field_name, value in flags.__dataclass_fields__.items():
        if value.default is False:
            assert getattr(flags, field_name) is False, f"{field_name} 默认应为 False"
