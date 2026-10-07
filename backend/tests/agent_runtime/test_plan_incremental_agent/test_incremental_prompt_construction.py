"""Test: 16. prompt 构建不泄漏 CoT,JSON 严格模式,无 markdown."""

from __future__ import annotations

import pytest

from app.agent_runtime.incremental.prompt import (
    INCREMENTAL_SYSTEM_PROMPT,
    build_incremental_prompt,
)

from .conftest import build_intent, build_state


def test_incremental_system_prompt_contains_banned_tools():
    assert "BANNED TOOLS" in INCREMENTAL_SYSTEM_PROMPT or "禁止" in INCREMENTAL_SYSTEM_PROMPT


def test_incremental_system_prompt_has_anti_prompt_injection():
    text = INCREMENTAL_SYSTEM_PROMPT.lower()
    assert "injection" in text or "ignore" in text or "指令" in INCREMENTAL_SYSTEM_PROMPT


def test_build_incremental_prompt_returns_system_and_user():
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)
    locked = list(state.get("locked_section_ids") or [])
    system, user = build_incremental_prompt(
        intent=intent, locked_section_ids=locked, prior_steps=[],
    )
    assert isinstance(system, str) and len(system) > 100
    assert isinstance(user, str) and len(user) > 0


def test_incremental_prompt_no_raw_section_content_in_system():
    """system prompt 应该是指令型文本,不含 section 真实内容(脱敏)。"""
    secret = "秘密需求描述:用户密码是 12345"
    intent = build_intent(
        target_section_ids=["s1"], kind="modify_section",
        raw_user_message=secret,
    )
    state = build_state(intent=intent)
    locked = list(state.get("locked_section_ids") or [])
    system, _ = build_incremental_prompt(
        intent=intent, locked_section_ids=locked, prior_steps=[],
    )
    # system prompt 应当是 SSOT 模板,不含 user 数据
    assert "12345" not in system
    assert "密码" not in system


def test_incremental_prompt_user_content_includes_modification_summary():
    """user content 应包含 request_text(modification_summary 等价载体)。"""
    intent = build_intent(
        target_section_ids=["s1"],
        kind="modify_section",
        raw_user_message="修改章节 1 的表格列宽",
    )
    state = build_state(intent=intent)
    locked = list(state.get("locked_section_ids") or [])
    _, user = build_incremental_prompt(
        intent=intent, locked_section_ids=locked, prior_steps=[],
    )
    # user content 应包含 modification 摘要 + target section id
    assert "s1" in user
    assert "修改章节 1 的表格列宽" in user