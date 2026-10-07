"""TemplateParserTool — parses test-plan Word templates.

Reads a .docx template file and extracts:
- Section hierarchy (headings detected via style / outline / numbering)
- Table schemas (headers, row/col counts, complexity markers)
- Section descriptions (italicised guidance text)
- Recommended generation mode per section (ai / keep / manual)
- Field-name mapping (Chinese → English) and JSON schema preview
- Generation config (ai_fields, keep_sections, manual_sections, field_bindings)

All 22 capabilities from the legacy TemplateSectionService +
TemplatePromptPreviewService are preserved.

════════════════════════════════════════════════════════════════════════════════
链路位置 (Phase 2.1 起就被 8-Tool 链路调用):

  节点 parse_template_node
    → TemplateParserTool.run(inputs={"template_file_id": ...})
      → 走 app.common.template_section_service + template_prompt_preview_service
      → 写入 state.template_structure(sections / table_schemas / ai_fields /
         keep_sections / field_bindings)
      → 同时写入 state.review_standard,供下游 ResultReviewTool 使用

调用合约(供开发者速查):
  - inputs 必须含 template_file_id(从 ctx.template_file_id 取);
  - 失败 → 返回 _error("TEMPLATE_PARSE_FAILED" / "TEMPLATE_FILE_MISSING" /
    "TEMPLATE_SECTION_MISSING" 等),由 parse_template_node 写入 last_error,
    route_after_parse_template / parse_template_node 自身根据 task_status 决定
    是否 fail_task;
  - 成功 → 输出 envelope.public_execution_update 把 section 摘要推到前端
    (同时也是 barrier_path_map 中 KB / prep / suggest_sections 的入口)。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.common.template_section import TemplateParseResult
from app.common.template_section_service import TemplateSectionService
from app.common.template_prompt_preview_service import TemplatePromptPreviewService
from app.storage.file_reference_resolver import (
    ResolvedFileReference,
    resolve_file_reference_sync,
)
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class TemplateParserTool(BaseTool):
    name = "TemplateParserTool"
    description = "解析测试方案 Word 模板，识别章节层级、表格结构和可生成区域"

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        template_file_id = inputs.get("template_file_id", "")
        parse_options: dict = inputs.get("parse_options", {}) or {}

        if not template_file_id:
            return self._error(
                "TEMPLATE_FILE_NOT_FOUND",
                "缺少 template_file_id 参数",
                recoverable=False,
            )

        # Resolve file path via shared Resolver (Phase 2.9A.13)
        resolved = resolve_file_reference_sync(
            template_file_id,
            expected_role=None,  # TemplateParser 不强校验 role
            user_internal_id=getattr(context, "user_internal_id", None),
            conversation_internal_id=getattr(context, "conversation_internal_id", None),
            task_internal_id=getattr(context, "task_internal_id", None),
        )
        if not resolved.exists or not resolved.absolute_path:
            return self._error(
                "TEMPLATE_FILE_NOT_FOUND",
                f"模板文件不存在（{resolved.failure_stage}）",
                recoverable=False,
            )
        file_path = Path(resolved.absolute_path)
        if not file_path.exists():
            return self._error(
                "TEMPLATE_FILE_NOT_FOUND",
                "模板文件未找到，请重新上传",
                recoverable=False,
            )

        # Parse template
        service = TemplateSectionService()
        prompt_preview = TemplatePromptPreviewService()

        try:
            await self._progress(context, "正在读取测试方案模板")
            parse_result: TemplateParseResult = await asyncio.to_thread(
                service.parse_template, str(file_path)
            )
        except ValueError as exc:
            msg = str(exc)
            if "标题层级" in msg:
                return self._error(
                    "TEMPLATE_STRUCTURE_NOT_FOUND",
                    msg,
                    recoverable=False,
                )
            logger.warning("TemplateParserTool: 模板解析失败 | 文件=%s | err=%s", Path(file_path).name, msg[:200])
            return self._error(
                "TEMPLATE_PARSE_FAILED",
                msg,
                recoverable=False,
            )
        except FileNotFoundError:
            return self._error(
                "TEMPLATE_FILE_NOT_FOUND",
                "模板文件不存在，请重新上传",
                recoverable=False,
            )
        except Exception as exc:
            logger.exception("模板解析失败")
            return self._error(
                "TEMPLATE_PARSE_FAILED",
                f"模板解析失败：{exc}",
                recoverable=False,
            )

        # Build generation config
        await self._progress(context, "正在识别模板章节结构")
        all_sections = parse_result.all_sections()
        await self._progress(context, "正在整理模板结构")
        generation_config = prompt_preview.build_generation_config(parse_result.sections)

        # Build the Tool output data
        sections_data = [s.to_dict() for s in parse_result.sections]
        self._attach_section_bindings(
            sections_data,
            generation_config.get("section_bindings") or [],
        )
        logger.info(
            "TemplateParserTool: top_level_sections=%d | all_sections=%d",
            len(parse_result.sections), len(all_sections),
        )

        # F025 — synthesise review_standard from the parsed template +
        # generation_config.  This drives the post-generation review
        # and regen loop.  It is written into ``template_structure``
        # so the orchestrator can lift it into ``ctx.review_standard``
        # in the pre-confirm step (or copy it after confirm).
        try:
            from app.common.template_section_service import build_review_standard
            template_file_id = (
                inputs.get("template_file_id")
                or getattr(context, "template_file_id", None)
            )
            review_standard = build_review_standard(
                template_id=template_file_id,
                sections=parse_result.sections,
                generation_config=generation_config,
            )
        except Exception as exc:
            logger.warning(
                "TemplateParserTool: build_review_standard failed, "
                "falling back to no standard | err=%s", exc,
            )
            review_standard = None

        # Count modes
        mode_counts = {"ai_generate": 0, "keep_template": 0, "manual_fill": 0, "skip": 0}
        for s in all_sections:
            if s.mode == "ai":
                mode_counts["ai_generate"] += 1
            elif s.mode == "keep":
                mode_counts["keep_template"] += 1
            elif s.mode == "manual":
                mode_counts["manual_fill"] += 1

        data = {
            "template_name": parse_result.template_name,
            "sections": sections_data,
            "generation_config": generation_config,
            "review_standard": review_standard,
            "summary": {
                "section_count": len(all_sections),
                "top_level_count": parse_result.top_level_count,
                "child_count": parse_result.child_count,
                "table_count": parse_result.table_count,
                "fixed_approval_count": parse_result.fixed_approval_count,
                "ai_generate_count": mode_counts["ai_generate"],
                "keep_template_count": mode_counts["keep_template"],
                "manual_fill_count": mode_counts["manual_fill"],
            },
        }

        # Write to AgentContext
        context.template_structure = data

        return self._success(
            data,
            f"测试方案模板解析完成：已识别 {len(all_sections)} 个章节、"
            f"{parse_result.table_count} 个表格",
        )

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _attach_section_bindings(
        sections: list[dict],
        bindings: list[dict],
    ) -> None:
        """Copy canonical generation identities onto the serialized tree.

        ``TemplateSection.to_dict`` contains document structure, while the
        canonical ``section_id`` and ``field`` are calculated later by
        ``TemplatePromptPreviewService``.  SectionSuggestionTool and the
        confirmation UI consume this serialized tree, so omitting those
        identities made the UI invent positional IDs such as ``section_12``.

        Both structures use the same depth-first traversal, so binding them in
        traversal order preserves the identity of nested sections as well.
        """
        flattened: list[dict] = []

        def visit(items: list[dict]) -> None:
            for section in items:
                if not isinstance(section, dict):
                    continue
                flattened.append(section)
                children = section.get("children") or []
                if isinstance(children, list):
                    visit(children)

        visit(sections)
        if len(flattened) != len(bindings):
            logger.warning(
                "TemplateParserTool: section/binding count mismatch | "
                "sections=%d | bindings=%d",
                len(flattened),
                len(bindings),
            )

        for section, binding in zip(flattened, bindings):
            if not isinstance(binding, dict):
                continue
            section["section_id"] = str(binding.get("section_id") or "")
            section["field"] = str(binding.get("field") or "")
            section["suggested_field"] = str(binding.get("suggested_field") or "")
            section["clean_title"] = str(binding.get("clean_title") or "")

    @staticmethod
    def _resolve_storage_path(file_id: str) -> str | None:
        """Deprecated — use :func:`resolve_file_reference_sync` instead.

        Kept only for legacy unit tests that asserted on the storage_path
        string directly.  Returns the relative storage path on success,
        ``None`` otherwise.  The new :class:`ResolvedFileReference` carries
        richer failure metadata; see ``app/storage/file_reference_resolver.py``.
        """
        resolved = resolve_file_reference_sync(file_id, expected_role=None)
        return resolved.storage_path or None
