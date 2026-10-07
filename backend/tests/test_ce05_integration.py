"""CE-05 WP-11 全量安全与集成测试。

覆盖：
  - 动态 Flag 直读扫描（Task Path 直读 Task-semantic = 0）
  - 独立安全规则扫描（Independent Secret/PII/Injection Outside = 0）
  - 23 项 Flag 完整性
  - Cross-worker Manifest 复用 / Resume 不读动态 env
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.context_engine.freeze.profiles import FROZEN_FLAG_KEYS


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════
# 1. 23 项 Flag 完整性
# ══════════════════════════════════════════════════════════════════

def test_frozen_flag_count_is_23():
    assert len(FROZEN_FLAG_KEYS) == 23


def test_frozen_flag_mig_count_is_8():
    mig = {k for k in FROZEN_FLAG_KEYS if k.startswith("MIG_")}
    assert len(mig) == 8


# ══════════════════════════════════════════════════════════════════
# 2. 动态 Flag 直读扫描（静态 AST/文本扫描）
# ══════════════════════════════════════════════════════════════════

def test_task_path_direct_dynamic_read_zero():
    """任务路径 Task-semantic/MIG flag 直读 = 0。

    扫描 app/（排除 test_/v2_frozen）中的
    get_context_engine_flags().(mig_|context_|memory_|retrieval_|rerank_|...)
    允许的仅 fallback else 分支（含 'else get_context_engine_flags'）。
    """
    root = _repo_root() / "app"
    forbidden = []
    for py in root.rglob("*.py"):
        if "test_" in py.name or "v2_frozen" in str(py):
            continue
        text = py.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            # 只查非 fallback 的直读（无 else 前缀）
            stripped = line.strip()
            if "get_context_engine_flags()" not in stripped:
                continue
            if stripped.startswith("else") or "else get_context_engine_flags" in stripped:
                continue
            if "context_debug" in str(py):
                continue  # Operational flag 门控，豁免
            forbidden.append(f"{py.relative_to(root)}:{line_no}: {stripped[:80]}")
    assert not forbidden, f"任务路径 Task-semantic 直读: {forbidden}"


def test_resume_no_dynamic_env_read():
    """Resume 只读 Manifest，不读动态 env（语义由 resolver 保证）。"""
    from app.context_engine.freeze.profiles import LEGACY_TASK_SEMANTIC_FLAGS
    from app.context_engine.freeze.resolver import TaskScopedFeatureFlagResolver
    from app.context_engine.freeze.service import build_manifest

    import os

    manifest = build_manifest(
        engine="langgraph", decision_reason="t", canary_bucket=None,
        workspace_key=None, context_engine_version="v3",
        task_semantic_flags={**LEGACY_TASK_SEMANTIC_FLAGS, "MIG_CHAT": False},
    )
    resolver = TaskScopedFeatureFlagResolver.from_manifest(manifest)
    os.environ["MIG_CHAT"] = "true"
    try:
        assert resolver.evaluate("MIG_CHAT") is False  # Manifest 冻结，非 env
    finally:
        os.environ.pop("MIG_CHAT", None)


# ══════════════════════════════════════════════════════════════════
# 3. 独立安全规则扫描
# ══════════════════════════════════════════════════════════════════

def test_independent_secret_sanitizers_outside_allowlist_zero():
    """独立 Secret sanitizer 仅允许 quality_validator（委托 security 包）。"""
    root = _repo_root() / "app"
    allowlisted = {
        "agent_runtime/_shared/narrative_governance/quality_validator.py",
        "context_engine/security",
    }
    hits = []
    for py in root.rglob("*.py"):
        rel = str(py.relative_to(root)).replace("\\", "/")
        if any(rel.startswith(a) for a in allowlisted):
            continue
        text = py.read_text(encoding="utf-8")
        if "def _sanitize_payload" in text or "def sanitize_text" in text:
            hits.append(rel)
    assert not hits, f"allowlist 外独立 secret sanitizer: {hits}"


def test_independent_pii_rules_outside_security_zero():
    """PII 规则仅允许 security/pii.py。"""
    root = _repo_root() / "app"
    hits = []
    for py in root.rglob("*.py"):
        rel = str(py.relative_to(root)).replace("\\", "/")
        if rel.startswith("context_engine/security"):
            continue
        text = py.read_text(encoding="utf-8")
        if "CONTEXT_PII_MODE" in text or "_DEFAULT_PII_PATTERNS" in text:
            hits.append(rel)
    assert not hits, f"security 外独立 PII 规则: {hits}"


def test_independent_injection_rules_outside_security_zero():
    """Injection 规则仅允许 injection_filter（规则源）+ security/injection（入口包装）。"""
    root = _repo_root() / "app"
    allowed = {"context_engine/selection/injection_filter.py", "context_engine/security"}
    hits = []
    for py in root.rglob("*.py"):
        rel = str(py.relative_to(root)).replace("\\", "/")
        if any(rel.startswith(a) for a in allowed):
            continue
        text = py.read_text(encoding="utf-8")
        if "_INJECTION_PATTERNS" in text and "def neutralize_text" in text:
            hits.append(rel)
    assert not hits, f"allowlist 外独立 injection 规则: {hits}"


def test_public_full_prompt_payload_creation_paths_zero():
    """无公共 Full Prompt Payload 新建路径（只读引用允许）。"""
    root = _repo_root() / "app"
    hits = []
    for py in root.rglob("*.py"):
        if "test_" in py.name or "v2_frozen" in str(py):
            continue
        text = py.read_text(encoding="utf-8")
        # 新建 full_prompt_payload 的赋值（排除 schema 定义与只读）
        if "full_prompt_payload_id" in text and ".values(" in text:
            hits.append(str(py.relative_to(root)))
        if "prompt_text[:1500]" in text or "prompt_text[:1000]" in text:
            hits.append(f"{py.relative_to(root)}: prompt_text 切片")
    assert not hits, f"Full Prompt 新建/切片路径: {hits}"
