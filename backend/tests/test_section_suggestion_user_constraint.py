"""Tests for F022 Step 3 — SectionSuggestionTool applying user constraints."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.context import AgentContext
from app.services.user_constraint_extractor import UserConstraintExtractor


# ── Fixtures ───────────────────────────────────────────────────────


def _ctx(
    *,
    sections: list[dict],
    user_prompt: str = "",
    settings_service=None,
) -> AgentContext:
    ctx = AgentContext(
        task_id="t1",
        conversation_id="c1",
        user_id="u1",
        user_internal_id=42,
    )
    ctx.template_structure = {"sections": sections}
    ctx.user_prompt = user_prompt
    ctx.settings_service = settings_service
    return ctx


def _stub_provider():
    return SimpleNamespace(is_unconfigured=False)


def _stub_settings_with_provider(provider):
    """settings_service stub exposing build_llm_config_provider."""
    async def _build(uid: int):
        return provider

    return SimpleNamespace(build_llm_config_provider=_build)


def _patch_extractor(monkeypatch, return_value: dict):
    """Replace ``UserConstraintExtractor.extract`` so tests don't need real LLM."""
    calls: list[tuple[str, list[str]]] = []

    async def fake_extract(self, user_prompt, section_titles):
        calls.append((user_prompt, list(section_titles)))
        return return_value

    monkeypatch.setattr(
        UserConstraintExtractor, "extract", fake_extract,
    )
    return calls


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_user_prompt_skips_extractor(monkeypatch):
    """No user prompt → no extractor call, suggestions unchanged."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    calls = _patch_extractor(monkeypatch, return_value={"测试设备": {"action": "keep_template", "reason": "用户"}})
    ctx = _ctx(
        sections=[
            {"title": "项目概述", "mode": "ai", "level": 0, "section_id": "s1"},
            {"title": "测试设备", "mode": "manual", "level": 0, "section_id": "s2"},
        ],
        user_prompt="",  # empty
    )
    tool = SectionSuggestionTool()
    result = await tool.run({}, ctx)

    assert result["success"] is True
    assert calls == []  # extractor was NOT called
    sections_out = result["data"]["sections"]
    # Neither got constraint_source
    assert all("constraint_source" not in s for s in sections_out)
    # Original mode mapping preserved
    assert sections_out[0]["suggested_action"] == "ai_generate"
    assert sections_out[1]["suggested_action"] == "manual_fill"


@pytest.mark.asyncio
async def test_no_settings_service_skips_extractor(monkeypatch):
    """Without settings_service we cannot resolve the LLM.  We still call
    the extractor — it has its own short-circuit for ``llm_provider=None``
    and returns ``{}`` — so the tool falls back to template defaults
    silently.  No exceptions propagate to the caller."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    calls = _patch_extractor(monkeypatch, return_value={})
    ctx = _ctx(
        sections=[
            {"title": "项目概述", "mode": "ai", "level": 0, "section_id": "s1"},
        ],
        user_prompt="测试设备保留原文",
        settings_service=None,
    )
    result = await SectionSuggestionTool().run({}, ctx)
    assert result["success"] is True
    assert len(calls) == 1  # extractor IS called with None provider
    section = result["data"]["sections"][0]
    assert section["suggested_action"] == "ai_generate"  # unchanged default


@pytest.mark.asyncio
async def test_explicit_keep_prompt_overrides_numbered_sections_without_llm():
    """User's explicit quoted section list must override card defaults."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    ctx = _ctx(
        sections=[
            {"title": "审批信息", "mode": "ai", "level": 0, "section_id": "s0"},
            {"title": "1 项目概述", "mode": "ai", "level": 0, "section_id": "s1"},
            {"title": "2 测试目标", "mode": "ai", "level": 0, "section_id": "s2"},
            {"title": "3 测试范围", "mode": "ai", "level": 0, "section_id": "s3"},
        ],
        user_prompt=(
            "帮我根据这份PRD需求文档和模板生成测试方案，"
            "其中的“项目概述”，“测试目标”章节保留原文"
        ),
        settings_service=None,
    )

    result = await SectionSuggestionTool().run({}, ctx)
    sections = {s["title"]: s for s in result["data"]["sections"]}

    assert result["success"] is True
    assert sections["1 项目概述"]["suggested_action"] == "keep_template"
    assert sections["1 项目概述"]["constraint_source"] == "user_prompt"
    assert sections["2 测试目标"]["suggested_action"] == "keep_template"
    assert sections["2 测试目标"]["constraint_source"] == "user_prompt"
    assert sections["审批信息"]["suggested_action"] == "ai_generate"
    assert "constraint_source" not in sections["审批信息"]
    assert ctx.user_constraints == {
        "1 项目概述": {"action": "keep_template", "reason": "用户要求保留原文"},
        "2 测试目标": {"action": "keep_template", "reason": "用户要求保留原文"},
    }


@pytest.mark.asyncio
async def test_happy_path_overrides_matching_sections(monkeypatch):
    """User says "测试设备保留原文" → that section flips to keep_template."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    _patch_extractor(
        monkeypatch,
        return_value={
            "测试设备": {"action": "keep_template", "reason": "用户要求保留原文"},
            "测试进度": {"action": "manual_fill", "reason": "用户要求手动填写"},
        },
    )
    ctx = _ctx(
        sections=[
            {"title": "项目概述", "mode": "ai", "level": 0, "section_id": "s1"},
            {"title": "测试设备", "mode": "manual", "level": 0, "section_id": "s2"},
            {"title": "测试进度", "mode": "ai", "level": 0, "section_id": "s3"},
        ],
        user_prompt="测试设备保留原文，测试进度我手动填",
        settings_service=_stub_settings_with_provider(_stub_provider()),
    )
    result = await SectionSuggestionTool().run({}, ctx)
    sections = {s["title"]: s for s in result["data"]["sections"]}

    # Override landed
    assert sections["测试设备"]["suggested_action"] == "keep_template"
    assert sections["测试设备"]["constraint_source"] == "user_prompt"
    assert "保留原文" in sections["测试设备"]["reason"]
    assert sections["测试进度"]["suggested_action"] == "manual_fill"
    assert sections["测试进度"]["constraint_source"] == "user_prompt"
    # Untouched
    assert sections["项目概述"]["suggested_action"] == "ai_generate"
    assert "constraint_source" not in sections["项目概述"]
    # Constraints recorded on context
    assert "测试设备" in ctx.user_constraints
    # Summary mentions the override count
    assert "2 个章节按用户提示词覆盖" in result["summary"]


@pytest.mark.asyncio
async def test_empty_constraints_keeps_defaults(monkeypatch):
    """Extractor returns ``{}`` → suggestions are untouched."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    _patch_extractor(monkeypatch, return_value={})
    ctx = _ctx(
        sections=[
            {"title": "项目概述", "mode": "ai", "level": 0, "section_id": "s1"},
        ],
        user_prompt="按默认的来就行",
        settings_service=_stub_settings_with_provider(_stub_provider()),
    )
    result = await SectionSuggestionTool().run({}, ctx)
    assert result["success"] is True
    section = result["data"]["sections"][0]
    assert section["suggested_action"] == "ai_generate"
    assert "constraint_source" not in section
    assert "按用户提示词覆盖" not in result["summary"]


@pytest.mark.asyncio
async def test_extractor_exception_swallowed(monkeypatch):
    """If the extractor itself raises, the tool still succeeds with template defaults."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    async def boom(self, user_prompt, section_titles):
        raise RuntimeError("LLM blew up")

    monkeypatch.setattr(UserConstraintExtractor, "extract", boom)
    ctx = _ctx(
        sections=[
            {"title": "项目概述", "mode": "ai", "level": 0, "section_id": "s1"},
        ],
        user_prompt="项目概述保留原文",
        settings_service=_stub_settings_with_provider(_stub_provider()),
    )
    result = await SectionSuggestionTool().run({}, ctx)
    assert result["success"] is True
    section = result["data"]["sections"][0]
    assert section["suggested_action"] == "ai_generate"
    assert "constraint_source" not in section


@pytest.mark.asyncio
async def test_settings_resolve_failure_swallowed(monkeypatch):
    """If settings_service.build_llm_config_provider throws, we still succeed
    by passing the failure's result (whatever it raised → caught → None) to
    the extractor; the extractor short-circuits to ``{}``."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    class BrokenSettings:
        async def build_llm_config_provider(self, uid: int):
            raise RuntimeError("DB down")

    calls = _patch_extractor(monkeypatch, return_value={})
    ctx = _ctx(
        sections=[
            {"title": "项目概述", "mode": "ai", "level": 0, "section_id": "s1"},
        ],
        user_prompt="用户说点什么",
        settings_service=BrokenSettings(),
    )
    result = await SectionSuggestionTool().run({}, ctx)
    assert result["success"] is True
    # The resolver failure was caught, extractor got None, returned {}
    assert len(calls) == 1
    assert result["data"]["sections"][0]["suggested_action"] == "ai_generate"


@pytest.mark.asyncio
async def test_action_normalisation_through_extractor(monkeypatch):
    """Extractor normalises aliases ('保留原文' → keep_template) before this layer.

    This test confirms that the normalised action from the extractor is what
    gets applied — the tool itself does not re-normalise.
    """
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    _patch_extractor(
        monkeypatch,
        return_value={
            "测试设备": {"action": "keep_template", "reason": "用户要求"},
        },
    )
    ctx = _ctx(
        sections=[
            {"title": "测试设备", "mode": "ai", "level": 0, "section_id": "s1"},
        ],
        user_prompt="测试设备保留原文",
        settings_service=_stub_settings_with_provider(_stub_provider()),
    )
    result = await SectionSuggestionTool().run({}, ctx)
    section = result["data"]["sections"][0]
    assert section["suggested_action"] == "keep_template"
    assert section["constraint_source"] == "user_prompt"


@pytest.mark.asyncio
async def test_nested_sections_can_be_overridden(monkeypatch):
    """Override matches by title even when the section is nested."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    _patch_extractor(
        monkeypatch,
        return_value={
            "测试设备清单": {"action": "keep_template", "reason": "用户要求保留原文"},
        },
    )
    ctx = _ctx(
        sections=[
            {
                "title": "第三章 测试准备",
                "mode": "ai",
                "level": 0,
                "section_id": "s1",
                "children": [
                    {"title": "测试设备清单", "mode": "manual", "level": 1, "section_id": "s2"},
                ],
            },
        ],
        user_prompt="测试设备清单保留原文",
        settings_service=_stub_settings_with_provider(_stub_provider()),
    )
    result = await SectionSuggestionTool().run({}, ctx)
    sections = {s["title"]: s for s in result["data"]["sections"]}
    assert sections["测试设备清单"]["suggested_action"] == "keep_template"
    assert sections["测试设备清单"]["constraint_source"] == "user_prompt"
    # Parent section untouched
    assert "constraint_source" not in sections["第三章 测试准备"]


@pytest.mark.asyncio
async def test_section_suggestions_stored_on_context(monkeypatch):
    """ctx.section_suggestions must still be populated for downstream consumers."""
    from app.tools.section_suggestion_tool import SectionSuggestionTool

    _patch_extractor(
        monkeypatch,
        return_value={"测试设备": {"action": "keep_template", "reason": "用户"}},
    )
    ctx = _ctx(
        sections=[
            {"title": "项目概述", "mode": "ai", "level": 0, "section_id": "s1"},
            {"title": "测试设备", "mode": "manual", "level": 0, "section_id": "s2"},
        ],
        user_prompt="测试设备保留",
        settings_service=_stub_settings_with_provider(_stub_provider()),
    )
    await SectionSuggestionTool().run({}, ctx)

    assert ctx.section_suggestions is not None
    assert ctx.section_suggestions["total"] == 2
    assert len(ctx.section_suggestions["sections"]) == 2
    # The store on ctx reflects the post-override state
    titles = [s["title"] for s in ctx.section_suggestions["sections"]]
    assert titles == ["项目概述", "测试设备"]


# ── Run ────────────────────────────────────────────────────────────


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
