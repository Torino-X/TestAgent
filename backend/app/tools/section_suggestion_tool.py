"""SectionSuggestionTool — generates chapter handling suggestions.

Combines two sources:

1. Template structure (``context.template_structure``) — the default
   per-section mode (``ai`` / ``keep`` / ``manual``) drives the
   initial suggestion.
2. User prompt (``context.user_prompt``) — F022
   :class:`UserConstraintExtractor` parses the user's natural-language
   instructions (e.g. "测试设备保留原文，不使用AI生成") and overrides
   the default for any matching section.

Sections that the user did NOT mention fall back to the template
default.  All overrides are flagged with ``constraint_source`` so
the UI can show a "用户指定" badge.

Supports actions: ``ai_generate``, ``keep_template``, ``manual_fill``, ``skip``.

════════════════════════════════════════════════════════════════════════════════
链路位置 (Phase 2.1 起就被 8-Tool 链路调用):

  节点 suggest_sections_node
    → SectionSuggestionTool.run(inputs={})   ← 无外部 inputs,直接从 ctx 取
      → 合并 template_structure 默认模式 + user_prompt 约束
      → 写入 state.section_suggestions = { "sections": [{section_id, title,
         suggestedAction, constraint_source, ...}] }

调用合约(供开发者速查):
  - 本工具不是 CORE_TOOLS 之一(失败不直接 fail_task);
  - 成功后 barrier → interrupt 节点(prepare_section_confirmation +
    section_confirmation_interrupt)走 prepare 节点 → interrupt 等待用户决策;
  - suggestions 列表每项的 action 取值受 schema 约束(LLM 生成章节 = ai_generate,
    保留模板原文 = keep_template, 人工填写 = manual_fill, 跳过 = skip)。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.services.user_constraint_extractor import UserConstraintExtractor
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)

# Map template-section modes to suggestion actions
_MODE_TO_ACTION = {
    "ai": "ai_generate",
    "keep": "keep_template",
    "manual": "manual_fill",
}


class SectionSuggestionTool(BaseTool):
    name = "SectionSuggestionTool"
    description = "根据需求分析、模板结构和知识库结果，生成章节处理建议"

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        template_structure = context.template_structure or {}
        sections = template_structure.get("sections", [])
        logger.info(
            "SectionSuggestionTool: template_structure_keys=%s | sections_count=%d",
            list(template_structure.keys()), len(sections),
        )
        await self._progress(context, "正在分析模板章节结构")

        if not sections:
            # Fallback: no template parsed yet — return empty with warning
            return self._success(
                {"sections": []},
                "未检测到模板章节，请先上传并解析模板文件",
                warnings=["缺少模板结构：请先运行 TemplateParserTool"],
            )

        await self._progress(context, "正在生成章节建议")
        suggestions = self._build_suggestions(sections)

        # F022 Step 3: overlay user-prompt constraints on top of the
        # template defaults.  Failures here are silent — the extractor
        # already swallows LLM errors and returns ``{}``, so a degraded
        # prompt is no worse than the pre-F022 behaviour.
        constraints = await self._extract_user_constraints(context, suggestions)
        if constraints:
            suggestions = self._apply_constraints(suggestions, constraints)
            context.user_constraints = constraints

        await self._progress(context, "正在整理建议结果")
        action_counts = self._count_actions(suggestions)

        context.section_suggestions = {
            "sections": suggestions,
            "total": len(suggestions),
        }

        summary = (
            f"章节处理建议已生成：共 {len(suggestions)} 个章节，"
            f"建议 AI 生成 {action_counts.get('ai_generate', 0)} 个、"
            f"保留模板 {action_counts.get('keep_template', 0)} 个、"
            f"手动填写 {action_counts.get('manual_fill', 0)} 个"
            + (
                f"、跳过 {action_counts.get('skip', 0)} 个"
                if action_counts.get("skip", 0)
                else ""
            )
        )
        if constraints:
            summary += f"（{len(constraints)} 个章节按用户提示词覆盖）"

        return self._success({"sections": suggestions}, summary)

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    async def _extract_user_constraints(
        context: AgentContext,
        suggestions: list[dict],
    ) -> dict:
        """Run the F022 extractor over the user's prompt.

        Returns ``{canonical_section_title: {action, reason}}`` — may be
        empty if the user didn't mention any section, or if the LLM is
        not configured, or if the call failed.  All branches must
        return ``{}`` rather than raising — this is a best-effort
        overlay on top of the template default.
        """
        user_prompt = (context.user_prompt or "").strip()
        if not user_prompt:
            logger.info("SectionSuggestionTool: user_prompt 为空，跳过用户约束抽取")
            return {}
        section_titles = [
            s.get("title", "") for s in suggestions if s.get("title")
        ]
        if not section_titles:
            logger.info("SectionSuggestionTool: section_titles 为空，跳过用户约束抽取")
            return {}
        logger.info(
            "SectionSuggestionTool: 准备抽取用户章节约束 | prompt_len=%d | section_titles=%d",
            len(user_prompt),
            len(section_titles),
        )

        llm_provider = await SectionSuggestionTool._resolve_llm_provider(context)
        # CE-04 §四：透传 context_llm_invoker（_AgentContextProxy 已带该字段）。
        # MIG_PREPARATION=true 时 extractor 内部走 bridge；不可用则跳过约束抽取。
        extractor = UserConstraintExtractor(
            llm_provider=llm_provider,
            context_llm_invoker=getattr(context, "context_llm_invoker", None),
            user_internal_id=getattr(context, "user_internal_id", 0) or 0,
            task_flag_resolver=getattr(context, "task_flag_resolver", None),
            task_id=getattr(context, "task_id", None),
            conversation_id=getattr(context, "conversation_internal_id", None),
            # The adapter provides the full live RuntimeContext explicitly.
            # Unit/legacy callers do not, in which case their context view is
            # still sufficient for deterministic fallback behaviour.
            runtime_context=getattr(context, "runtime_context", None) or context,
        )
        try:
            constraints = await extractor.extract(user_prompt, section_titles)
            logger.info(
                "SectionSuggestionTool: 用户章节约束抽取完成 | count=%d | constraints=%s",
                len(constraints),
                constraints,
            )
            return constraints
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SectionSuggestionTool: 用户约束抽取异常，已忽略 | err=%s",
                str(exc)[:200],
            )
            return {}

    @staticmethod
    async def _resolve_llm_provider(context: AgentContext):
        """Resolve the user's LLM provider via ``context.settings_service``.

        Returns ``None`` (or the unconfigured marker) when the user
        hasn't set up a model — the extractor treats both as "skip".
        Any exception is also swallowed: we never want a config read
        error to break the suggestion step.
        """
        settings = getattr(context, "settings_service", None)
        if settings is None:
            return None
        user_internal_id = int(getattr(context, "user_internal_id", 0) or 0)
        if user_internal_id <= 0:
            return None
        try:
            return await settings.build_llm_config_provider(user_internal_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SectionSuggestionTool: LLM provider 解析失败 | err=%s",
                str(exc)[:200],
            )
            return None

    @staticmethod
    def _apply_constraints(
        suggestions: list[dict],
        constraints: dict,
    ) -> list[dict]:
        """Overlay ``constraints`` on top of ``suggestions`` in-place.

        Each overridden suggestion gets three new fields:
        ``suggested_action`` (replaced), ``reason`` (replaced with a
        user-anchored sentence), and ``constraint_source`` =
        ``"user_prompt"``.  Unmatched sections are left untouched.
        """
        overridden = 0
        for s in suggestions:
            title = s.get("title", "")
            if title in constraints:
                override = constraints[title]
                action = override.get("action") or "ai_generate"
                reason_text = override.get("reason") or "用户要求"
                s["suggested_action"] = action
                s["reason"] = f"「{title}」{reason_text}"
                s["constraint_source"] = "user_prompt"
                overridden += 1
        if overridden:
            logger.info(
                "SectionSuggestionTool: 用户约束覆盖 %d/%d 个章节",
                overridden, len(suggestions),
            )
        return suggestions

    @staticmethod
    def _build_suggestions(sections: list[dict]) -> list[dict]:
        """Walk the template section tree and produce suggestion entries."""
        result: list[dict] = []

        def walk(sec: dict, path: str = "") -> None:
            title = sec.get("title", "")
            level = sec.get("level", 0)
            mode = sec.get("mode", "ai")
            section_id = sec.get("section_id", "")

            action = _MODE_TO_ACTION.get(mode, "ai_generate")
            reason = _reason_for_mode(mode, title)

            # Determine available_actions — keep/manual sections can be
            # overridden; ai sections are the recommendation.
            if mode == "keep":
                available = ["ai_generate", "keep_template", "manual_fill", "skip"]
            elif mode == "manual":
                available = ["ai_generate", "keep_template", "manual_fill", "skip"]
            else:
                available = ["ai_generate", "keep_template", "manual_fill", "skip"]

            result.append(
                {
                    "section_id": section_id,
                    "code": _section_code(path, title),
                    "title": title,
                    "level": f"L{level}",
                    "suggested_action": action,
                    "reason": reason,
                    "available_actions": available,
                }
            )

            for child in sec.get("children", []) or []:
                if isinstance(child, dict):
                    child_path = f"{path}/{title}" if path else title
                    walk(child, child_path)

        for section in sections:
            if isinstance(section, dict):
                walk(section)

        return result

    @staticmethod
    def _count_actions(suggestions: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in suggestions:
            action = s.get("suggested_action", "ai_generate")
            counts[action] = counts.get(action, 0) + 1
        return counts


def _reason_for_mode(mode: str, title: str) -> str:
    """Provide a human-readable reason for the suggested action."""
    if mode == "ai":
        return f"「{title}」建议由 AI 根据需求文档生成"
    elif mode == "keep":
        return f"「{title}」模板原文已标准化，建议保留"
    elif mode == "manual":
        return f"「{title}」含项目具体信息，建议手动填写"
    return f"「{title}」建议跳过"


def _section_code(path: str, title: str) -> str:
    """Derive a section numbering code from path and title.

    Uses a simple counter-based approach — the TemplateParserTool provides
    richer numbering; this is a fallback.
    """
    # Try to extract numbering from title like "1.2.3 Title"
    import re

    m = re.match(r"^(\d+(?:\.\d+)*)\s", title)
    if m:
        return m.group(1)
    # Use path depth as hint
    if path:
        depth = path.count("/") + 1
        return str(depth)
    return "1"
