"""Unit tests for F022 UserConstraintExtractor."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest


# ── Pure-helper tests (no LLM call) ───────────────────────────────────


class TestMatchSectionTitle:
    """Section-title matching is the trickiest piece — test it directly."""

    def test_exact_match_after_strip(self):
        from app.services.user_constraint_extractor import match_section_title

        titles = ["项目概述", "测试设备", "测试进度"]
        assert match_section_title("测试设备", titles) == "测试设备"

    def test_numeric_prefix_strip(self):
        from app.services.user_constraint_extractor import match_section_title

        titles = ["1. 项目概述", "3.2 测试设备清单", "5. 测试进度"]
        # LLM returns without prefix
        assert match_section_title("测试设备清单", titles) == "3.2 测试设备清单"
        # LLM returns with prefix
        assert match_section_title("3.2 测试设备清单", titles) == "3.2 测试设备清单"

    def test_substring_canonical_in_llm(self):
        """LLM said '测试设备' — matches canonical '3.2 测试设备清单'."""
        from app.services.user_constraint_extractor import match_section_title

        titles = ["3.2 测试设备清单"]
        assert match_section_title("测试设备", titles) == "3.2 测试设备清单"

    def test_substring_llm_in_canonical(self):
        """LLM said '测试设备清单' — canonical '3.2 测试设备清单' contains it."""
        from app.services.user_constraint_extractor import match_section_title

        titles = ["3.2 测试设备清单"]
        assert match_section_title("测试设备清单", titles) == "3.2 测试设备清单"

    def test_no_match_returns_none(self):
        from app.services.user_constraint_extractor import match_section_title

        titles = ["项目概述", "测试范围"]
        assert match_section_title("人员安排", titles) is None

    def test_empty_inputs(self):
        from app.services.user_constraint_extractor import match_section_title

        assert match_section_title("", ["项目概述"]) is None
        assert match_section_title("项目概述", []) is None

    def test_skips_non_string_entries(self):
        """Robustness: bad data in section_titles shouldn't crash."""
        from app.services.user_constraint_extractor import match_section_title

        titles = [None, "", "项目概述", 123]
        assert match_section_title("项目概述", titles) == "项目概述"


class TestCoerceAction:
    def test_canonical_values_pass_through(self):
        from app.services.user_constraint_extractor import _coerce_action

        assert _coerce_action("ai_generate") == "ai_generate"
        assert _coerce_action("keep_template") == "keep_template"
        assert _coerce_action("manual_fill") == "manual_fill"

    def test_aliases_normalised(self):
        from app.services.user_constraint_extractor import _coerce_action

        assert _coerce_action("ai") == "ai_generate"
        assert _coerce_action("keep") == "keep_template"
        assert _coerce_action("manual") == "manual_fill"
        assert _coerce_action("保留原文") == "keep_template"
        assert _coerce_action("手动填写") == "manual_fill"

    def test_non_string_returns_none(self):
        from app.services.user_constraint_extractor import _coerce_action

        assert _coerce_action(None) is None
        assert _coerce_action(123) is None
        assert _coerce_action([]) is None

    def test_unknown_returns_none(self):
        from app.services.user_constraint_extractor import _coerce_action

        assert _coerce_action("rebuild_from_scratch") is None
        assert _coerce_action("??") is None


# ── Service-level tests (mocked LLM) ──────────────────────────────────


class _MockLLMClient:
    """Stand-in for LLMClient that returns a canned string."""

    def __init__(
        self,
        response: str,
        raise_exc: Exception | None = None,
        calls: list[dict] | None = None,
    ) -> None:
        self._response = response
        self._raise = raise_exc
        self._calls = calls

    async def generate_with_system(self, **kwargs) -> str:
        if self._calls is not None:
            self._calls.append(dict(kwargs))
        if self._raise is not None:
            raise self._raise
        return self._response


class _StubProvider:
    """Marker-compatible provider so the service treats it as configured."""

    is_unconfigured = False


@pytest.fixture
def _noop():
    """Placeholder kept for symmetry with future fixture needs."""


class TestUserConstraintExtractor:
    @pytest.mark.asyncio
    async def test_empty_prompt_returns_empty(self):
        from app.services.user_constraint_extractor import UserConstraintExtractor

        ext = UserConstraintExtractor(llm_provider=_StubProvider())
        assert await ext.extract("", ["项目概述"]) == {}
        assert await ext.extract("   ", ["项目概述"]) == {}

    @pytest.mark.asyncio
    async def test_empty_sections_returns_empty(self):
        from app.services.user_constraint_extractor import UserConstraintExtractor

        ext = UserConstraintExtractor(llm_provider=_StubProvider())
        assert await ext.extract("测试设备保留原文", []) == {}

    @pytest.mark.asyncio
    async def test_unconfigured_provider_still_extracts_explicit_constraint(self):
        from app.core.llm_not_configured import LLMNotConfiguredMarker
        from app.services.user_constraint_extractor import UserConstraintExtractor

        ext = UserConstraintExtractor(llm_provider=LLMNotConfiguredMarker())
        assert await ext.extract("测试设备保留原文", ["测试设备"]) == {
            "测试设备": {"action": "keep_template", "reason": "用户要求保留原文"},
        }

    @pytest.mark.asyncio
    async def test_no_provider_returns_empty(self):
        from app.services.user_constraint_extractor import UserConstraintExtractor

        ext = UserConstraintExtractor()
        assert await ext.extract("测试设备按项目情况处理", ["测试设备"]) == {}

    @pytest.mark.asyncio
    async def test_explicit_keep_constraint_does_not_require_llm(self):
        from app.services.user_constraint_extractor import UserConstraintExtractor

        ext = UserConstraintExtractor()
        result = await ext.extract(
            "帮我根据这份PRD需求文档和模板生成测试方案，其中的“项目概述”，“测试目标”章节保留原文",
            ["审批信息", "1 项目概述", "2 测试目标", "3 测试范围"],
        )

        assert result == {
            "1 项目概述": {"action": "keep_template", "reason": "用户要求保留原文"},
            "2 测试目标": {"action": "keep_template", "reason": "用户要求保留原文"},
        }

    @pytest.mark.asyncio
    async def test_explicit_keep_constraint_uses_context_engine_before_rule_fallback(self, monkeypatch):
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "项目概述", "action": "keep_template", "reason": "LLM识别保留"},
                    {"section_title": "测试目标", "action": "keep_template", "reason": "LLM识别保留"},
                ],
            },
            ensure_ascii=False,
        )
        bridge = _FakeBridge(payload)
        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=bridge,
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "帮我根据这份PRD需求文档和模板生成测试方案，其中的“项目概述”，“测试目标”章节保留原文",
            ["审批信息", "1 项目概述", "2 测试目标", "3 测试范围"],
        )

        assert bridge.calls == 1
        assert "模板章节标题列表" in bridge.user_content
        assert result == {
            "1 项目概述": {"action": "keep_template", "reason": "LLM识别保留"},
            "2 测试目标": {"action": "keep_template", "reason": "LLM识别保留"},
        }

    @pytest.mark.asyncio
    async def test_context_engine_constraint_call_keeps_task_identity(self):
        """The section-suggestion CE profile requires task state.

        The bridge must receive the active task and runtime context; otherwise
        TaskStateSourceAdapter reports ``context.source.no_task`` and the
        extractor silently loses the user's explicit section constraints.
        """
        from app.services.user_constraint_extractor import UserConstraintExtractor

        bridge = _FakeBridge(json.dumps({"constraints": []}, ensure_ascii=False))
        runtime_context = SimpleNamespace(
            task_internal_id=280,
            conversation_internal_id=453,
            session_factory=object(),
        )
        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=bridge,
            task_flag_resolver=_PreparationResolver(True),
            task_id="task_48f65c7b",
            conversation_id=453,
            runtime_context=runtime_context,
        )

        await ext.extract("按默认规则生成", ["项目概述"])

        assert bridge.task_id == "task_48f65c7b"
        assert bridge.conversation_id == 453
        assert bridge.runtime_context is runtime_context

    @pytest.mark.asyncio
    async def test_explicit_keep_constraint_falls_back_when_context_engine_returns_empty(self, monkeypatch):
        from app.services.user_constraint_extractor import UserConstraintExtractor

        bridge = _FakeBridge(json.dumps({"constraints": []}, ensure_ascii=False))
        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=bridge,
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "帮我根据这份PRD需求文档和模板生成测试方案，其中的“项目概述”，“测试目标”章节保留原文",
            ["审批信息", "1 项目概述", "2 测试目标", "3 测试范围"],
        )

        assert bridge.calls == 1
        assert result == {
            "1 项目概述": {"action": "keep_template", "reason": "用户要求保留原文"},
            "2 测试目标": {"action": "keep_template", "reason": "用户要求保留原文"},
        }

    @pytest.mark.asyncio
    async def test_llm_error_returns_empty(self, monkeypatch):
        from app.services.user_constraint_extractor import UserConstraintExtractor

        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=_FakeBridge("__raise__"),
            task_flag_resolver=_PreparationResolver(True),
        )
        assert await ext.extract("测试设备按项目情况处理", ["测试设备"]) == {}

    @pytest.mark.asyncio
    async def test_json_parse_failure_returns_empty(self, monkeypatch):
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        monkeypatch.setattr(
            mod, "LLMClient",
            lambda config_provider=None: _MockLLMClient("not json at all, sorry"),
        )

        ext = UserConstraintExtractor(llm_provider=_StubProvider())
        assert await ext.extract("测试设备按项目情况处理", ["测试设备"]) == {}

    @pytest.mark.asyncio
    async def test_happy_path_exact_match(self, monkeypatch):
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "keep_template", "reason": "用户要求保留原文"},
                ],
            },
            ensure_ascii=False,
        )

        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=_FakeBridge(payload),
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "测试设备保留原文",
            ["项目概述", "测试设备", "测试进度"],
        )
        assert result == {
            "测试设备": {"action": "keep_template", "reason": "用户要求保留原文"},
        }

    @pytest.mark.asyncio
    async def test_action_aliases_normalised(self, monkeypatch):
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "保留原文", "reason": "用户要求保留"},
                    {"section_title": "测试进度", "action": "手动填写", "reason": "用户要求手动"},
                ],
            },
            ensure_ascii=False,
        )

        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=_FakeBridge(payload),
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "测试设备保留原文，测试进度我手动填",
            ["测试设备", "测试进度"],
        )
        assert result["测试设备"]["action"] == "keep_template"
        assert result["测试进度"]["action"] == "manual_fill"

    @pytest.mark.asyncio
    async def test_numeric_prefix_strip_in_llm_output(self, monkeypatch):
        """LLM echoes the title with numeric prefix — extractor still matches."""
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "3.2 测试设备清单", "action": "keep_template", "reason": ""},
                ],
            },
            ensure_ascii=False,
        )

        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=_FakeBridge(payload),
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "测试设备清单保留原文",
            ["3.2 测试设备清单"],
        )
        assert "3.2 测试设备清单" in result
        assert result["3.2 测试设备清单"]["action"] == "keep_template"

    @pytest.mark.asyncio
    async def test_unmatched_title_silently_dropped(self, monkeypatch):
        """LLM invents a section that doesn't exist — we drop it."""
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "keep_template", "reason": "保留"},
                    {"section_title": "不存在的章节", "action": "manual_fill", "reason": "手动"},
                ],
            },
            ensure_ascii=False,
        )

        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=_FakeBridge(payload),
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "测试设备保留，不存在的章节手动",
            ["测试设备"],
        )
        assert "测试设备" in result
        assert "不存在的章节" not in result

    @pytest.mark.asyncio
    async def test_invalid_action_dropped(self, monkeypatch):
        """LLM returns an action outside the enum — we drop it."""
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "rebuild", "reason": ""},
                    {"section_title": "测试进度", "action": "manual_fill", "reason": ""},
                ],
            },
            ensure_ascii=False,
        )

        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=_FakeBridge(payload),
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "测试设备重建，测试进度手动",
            ["测试设备", "测试进度"],
        )
        assert "测试设备" not in result  # dropped (unknown action)
        assert "测试进度" in result

    @pytest.mark.asyncio
    async def test_empty_constraints_list(self, monkeypatch):
        """User mentioned nothing — LLM returns empty list, we return {}."""
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps({"constraints": []}, ensure_ascii=False)

        monkeypatch.setattr(
            mod, "LLMClient",
            lambda config_provider=None: _MockLLMClient(payload),
        )

        ext = UserConstraintExtractor(llm_provider=_StubProvider())
        result = await ext.extract(
            "按你们默认的来就行",
            ["项目概述", "测试范围"],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_malformed_top_level_returns_empty(self, monkeypatch):
        """LLM returns array at top level — we don't crash."""
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps([{"section_title": "测试设备", "action": "keep_template"}])

        monkeypatch.setattr(
            mod, "LLMClient",
            lambda config_provider=None: _MockLLMClient(payload),
        )

        ext = UserConstraintExtractor(llm_provider=_StubProvider())
        result = await ext.extract(
            "测试设备保留",
            ["测试设备"],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_markdown_fenced_response(self, monkeypatch):
        """LLM wraps JSON in ```json ... ``` — extractor handles it."""
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = (
            "```json\n"
            + json.dumps(
                {
                    "constraints": [
                        {"section_title": "测试设备", "action": "keep_template", "reason": ""},
                    ],
                },
                ensure_ascii=False,
            )
            + "\n```"
        )

        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=_FakeBridge(payload),
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "测试设备保留",
            ["测试设备"],
        )
        assert result == {"测试设备": {"action": "keep_template", "reason": ""}}

    @pytest.mark.asyncio
    async def test_duplicate_titles_last_wins(self, monkeypatch):
        """LLM emits same canonical title twice — last action wins."""
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "keep_template", "reason": "first"},
                    {"section_title": "测试设备", "action": "manual_fill", "reason": "second"},
                ],
            },
            ensure_ascii=False,
        )

        ext = UserConstraintExtractor(
            llm_provider=_StubProvider(),
            context_llm_invoker=_FakeBridge(payload),
            task_flag_resolver=_PreparationResolver(True),
        )
        result = await ext.extract(
            "测试设备按先后要求处理。",
            ["测试设备"],
        )
        assert result["测试设备"]["action"] == "manual_fill"
        assert result["测试设备"]["reason"] == "second"


# ── CE-04 §四：MIG_PREPARATION 桥接路径 ────────────────────────────


class _FakeBridge:
    """Minimal ContextInvokerBridge stand-in for extractor tests."""

    def __init__(self, value, *, available=True):
        self._value = value
        self.available = available
        self.calls = 0
        self.call_site = None
        self.user_id = None
        self.profile = None
        self.user_content = None
        self.task_id = None
        self.conversation_id = None
        self.runtime_context = None

    async def generate(self, **kwargs):
        self.calls += 1
        self.call_site = kwargs.get("call_site")
        self.user_id = kwargs.get("user_id")
        self.profile = kwargs.get("llm_task_profile")
        self.user_content = kwargs.get("user_content")
        self.task_id = kwargs.get("task_id")
        self.conversation_id = kwargs.get("conversation_id")
        self.runtime_context = kwargs.get("runtime_context")
        if self._value == "__raise__":
            raise RuntimeError("bridge failed")
        from types import SimpleNamespace

        return SimpleNamespace(value=self._value)


def _patch_mig(monkeypatch, *, enabled: bool):
    from unittest.mock import patch

    from app.context_engine import feature_flags as ff
    from app.context_engine.feature_flags import ContextEngineFeatureFlags

    return patch.object(
        ff, "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(
            context_engine_agent_enabled=enabled,
            mig_preparation=enabled,
        ),
    )


class _PreparationResolver:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def evaluate(self, name: str) -> bool:
        assert name in {"CONTEXT_ENGINE_AGENT_ENABLED", "MIG_PREPARATION"}
        return self.enabled


class TestCe04BridgePath:
    @pytest.mark.asyncio
    async def test_mig_true_uses_bridge_not_legacy(self, monkeypatch):
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "keep_template", "reason": "保留"},
                ],
            },
            ensure_ascii=False,
        )
        bridge = _FakeBridge(payload)
        # legacy 若被误调应抛错 → 证明未回退
        monkeypatch.setattr(
            mod, "LLMClient",
            lambda config_provider=None: _MockLLMClient("legacy was called", raise_exc=AssertionError("legacy 被误调")),
        )
        with _patch_mig(monkeypatch, enabled=True):
            ext = UserConstraintExtractor(
                llm_provider=_StubProvider(),
                context_llm_invoker=bridge,
                user_internal_id=7,
                task_flag_resolver=_PreparationResolver(True),
            )
            result = await ext.extract("测试设备按要求处理", ["测试设备"])

        assert result == {"测试设备": {"action": "keep_template", "reason": "保留"}}
        assert bridge.calls == 1
        assert bridge.call_site == "test_plan.section_suggest"
        assert bridge.user_id == 7
        assert bridge.profile.name == "section.suggest.v1"
        assert "模板章节标题列表" in bridge.user_content

    @pytest.mark.asyncio
    async def test_mig_true_bridge_unavailable_uses_safe_rule_only_fallback(self, monkeypatch):
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        bridge = _FakeBridge(None, available=False)
        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "manual_fill", "reason": "legacy"},
                ],
            },
            ensure_ascii=False,
        )
        legacy_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "LLMClient",
            lambda config_provider=None: _MockLLMClient(payload, calls=legacy_calls),
        )
        with _patch_mig(monkeypatch, enabled=True):
            ext = UserConstraintExtractor(
                llm_provider=_StubProvider(),
                context_llm_invoker=bridge,
                task_flag_resolver=_PreparationResolver(True),
            )
            result = await ext.extract("测试设备按要求处理", ["测试设备"])

        assert result == {}
        assert bridge.calls == 0
        assert len(legacy_calls) == 0

    @pytest.mark.asyncio
    async def test_mig_true_bridge_returns_none_uses_safe_rule_only_fallback(self, monkeypatch):
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        bridge = _FakeBridge(None, available=True)  # value None → 失败
        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "manual_fill", "reason": "legacy"},
                ],
            },
            ensure_ascii=False,
        )
        legacy_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "LLMClient",
            lambda config_provider=None: _MockLLMClient(payload, calls=legacy_calls),
        )
        with _patch_mig(monkeypatch, enabled=True):
            ext = UserConstraintExtractor(
                llm_provider=_StubProvider(),
                context_llm_invoker=bridge,
                task_flag_resolver=_PreparationResolver(True),
            )
            result = await ext.extract("测试设备按要求处理", ["测试设备"])

        assert result == {}
        assert bridge.calls == 1
        assert len(legacy_calls) == 0

    @pytest.mark.asyncio
    async def test_mig_true_bridge_raises_uses_safe_rule_only_fallback(self, monkeypatch):
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        bridge = _FakeBridge("__raise__", available=True)
        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "manual_fill", "reason": "legacy"},
                ],
            },
            ensure_ascii=False,
        )
        legacy_calls: list[dict] = []
        monkeypatch.setattr(
            mod, "LLMClient",
            lambda config_provider=None: _MockLLMClient(payload, calls=legacy_calls),
        )
        with _patch_mig(monkeypatch, enabled=True):
            ext = UserConstraintExtractor(
                llm_provider=_StubProvider(),
                context_llm_invoker=bridge,
                task_flag_resolver=_PreparationResolver(True),
            )
            result = await ext.extract("测试设备按要求处理", ["测试设备"])

        assert result == {}
        assert len(legacy_calls) == 0

    @pytest.mark.asyncio
    async def test_disabled_migration_never_reenables_legacy_prompt_path(self, monkeypatch):
        from app.services import user_constraint_extractor as mod
        from app.services.user_constraint_extractor import UserConstraintExtractor

        payload = json.dumps(
            {
                "constraints": [
                    {"section_title": "测试设备", "action": "keep_template", "reason": "保留"},
                ],
            },
            ensure_ascii=False,
        )
        bridge = _FakeBridge(payload, available=True)
        monkeypatch.setattr(
            mod, "LLMClient",
            lambda config_provider=None: _MockLLMClient(payload),
        )
        with _patch_mig(monkeypatch, enabled=False):
            ext = UserConstraintExtractor(
                llm_provider=_StubProvider(),
                context_llm_invoker=bridge,
                task_flag_resolver=_PreparationResolver(False),
            )
            result = await ext.extract("测试设备按要求处理", ["测试设备"])

        assert result == {}
        assert bridge.calls == 0


# ── Run ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
