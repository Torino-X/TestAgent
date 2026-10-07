"""RequirementParserTool — parses uploaded requirement documents.

Reads .docx / .txt / .md files via the adapted DocumentReader and
(F020) feeds extracted images through the OCR + Vision pipeline so
that business-meaningful images actually reach the LLM.

F020 changes vs the previous version:

  * OCR is now always-on (the ``parse_options.extract_images`` switch
    is ignored — the parameter is preserved for back-compat callers).
  * The new ``ImageUnderstandingOrchestrator`` returns per-image
    ``ImageBlockResult`` objects; the tool then rebuilds
    ``text_content`` by replacing image blocks with their formatted
    representation (vision → structured JSON, OCR → plain text,
    otherwise → a clear "未能识别" marker).
  * ``data["_pending"]`` no longer claims ``vision_understanding`` —
    the orchestrator runs synchronously during this tool call.

════════════════════════════════════════════════════════════════════════════════
链路位置 (Phase 2.1 起就被 8-Tool 链路调用):

  节点 parse_requirement_node
    → RequirementParserTool.run(inputs={"requirement_file_id": ...})
      → app.common.document_reader 读 .docx / .txt / .md
      → (F020) ImageUnderstandingOrchestrator 把图片块转写为 vision JSON / OCR 文本 / 标记
      → 写入 state.requirement_analysis(text_content + 结构化字段 + 图片块摘要)
      → 写 fixture 状态字段 requirement_file_id / requirement_summary

调用合约(供开发者速查):
  - inputs 必须含 requirement_file_id(从 ctx.requirement_file_id 取);
  - 失败 → 返回 _error("REQUIREMENT_PARSE_FAILED" / "REQUIREMENT_FILE_MISSING" 等),
    parse_requirement_node 判定为 CORE_TOOLS 失败 → 写 last_error → fail_task;
  - 成功 → barrier 把 section_count / table_count / image_count / document_title
    等事实通过 narrative_composer.context_builders 暴露给前端叙事层。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.common.document_reader import (
    DocumentReader,
    DocumentReadError,
    DocumentReadResult,
    RequirementBlock,
)
from app.services.image_understanding_orchestrator import (
    ImageBlockResult,
    process_images,
)
from app.storage.local_storage import local_storage
from app.storage.oss_storage import object_storage
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class RequirementParserTool(BaseTool):
    name = "RequirementParserTool"
    description = "解析需求文档，提取标题结构、正文内容、表格、业务模块和功能点"

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        requirement_file_id = inputs.get("requirement_file_id", "")
        parse_options: dict = inputs.get("parse_options", {}) or {}
        # F020: ``extract_images`` is preserved for back-compat callers
        # but no longer gates OCR — OCR is always-on.  The value is
        # logged for traceability only.
        _legacy_extract_images = parse_options.get("extract_images", False)
        max_text_length = parse_options.get("max_text_length", 200_000)

        if not requirement_file_id:
            return self._error(
                "REQUIREMENT_FILE_NOT_FOUND",
                "缺少 requirement_file_id 参数",
                recoverable=False,
            )

        storage_path, storage_type = self._resolve_storage(requirement_file_id)
        if storage_path is None:
            return self._error(
                "REQUIREMENT_FILE_NOT_FOUND",
                f"需求文档不存在：{requirement_file_id}",
                recoverable=False,
            )

        descriptor, temporary_path = tempfile.mkstemp(suffix=Path(storage_path).suffix)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                if storage_type == "oss":
                    handle.write(await object_storage.read_bytes(storage_path))
                else:
                    # legacy / migrated rows that still point at local disk
                    with open(local_storage._base / storage_path, "rb") as src:
                        shutil.copyfileobj(src, handle)
        except Exception:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            return self._error(
                "REQUIREMENT_FILE_NOT_FOUND",
                "需求文档文件未找到，请重新上传",
                recoverable=False,
            )
        file_path = Path(temporary_path)

        reader = DocumentReader()
        try:
            if file_path.suffix.lower() == ".docx":
                await self._progress(context, "正在读取 Word 文档")
            else:
                await self._progress(context, "正在读取文档")
            result = await self._run_in_thread(reader.read_structured, str(file_path))
        except DocumentReadError as exc:
            if temporary_path:
                os.unlink(temporary_path)
            logger.warning(
                "RequirementParserTool: 解析失败 | 文件=%s | err=%s",
                Path(file_path).name, str(exc)[:200],
            )
            return self._error(
                "REQUIREMENT_PARSE_FAILED",
                str(exc),
                recoverable=False,
            )
        except Exception as exc:
            logger.exception("文档解析失败")
            return self._error(
                "REQUIREMENT_PARSE_FAILED",
                f"文档解析失败：{exc}",
                recoverable=False,
            )

        await self._progress(context, "正在解析文档标题和正文")
        if temporary_path:
            os.unlink(temporary_path)
        document_structure = self._build_document_structure(result.blocks)
        if any(block.block_type == "table" for block in result.blocks):
            await self._progress(context, "正在解析文档表格")
        table_summaries = self._build_table_summaries(result.blocks)

        # ── F020: always-on OCR + optional vision ────────────────
        img_config_provider = await self._resolve_image_config(context)
        if result.image_blocks:
            await self._progress(context, "正在提取文档图片")
            for image in result.image_blocks:
                image_name = Path(image.image_path).name if image.image_path else f"图片 {image.index}"
                await self._progress(context, f"正在处理图片：《{image_name}》")
            await self._progress(context, "正在识别图片文字")
        image_results = await process_images(
            result.blocks,
            img_config_provider=img_config_provider,
            context_llm_invoker=getattr(context, "context_llm_invoker", None),
            user_id=getattr(context, "user_internal_id", None),
            runtime_context=context,
        )

        # ── Build prompt-ready text (image positions preserved) ──
        await self._progress(context, "正在整理文档结构")
        text_content = self._build_text_content_with_images(result, image_results)

        original_len = len(text_content)
        text_content, warnings = self._apply_text_length_policy(
            text_content,
            warning_threshold=max_text_length,
        )
        if warnings:
            logger.info(
                "RequirementParserTool: 长文本保留全文 | 原始长度=%d | 提示阈值=%d",
                original_len, max_text_length,
            )

        image_texts = [
            {
                "image_index": r.index,
                "source": r.source,
                "ocr_text": r.ocr_text,
                "vision_summary": (r.vision.summary if r.vision else None),
            }
            for r in image_results.values()
        ]

        data = {
            "source_ref": requirement_file_id,
            "requirement_file_id": requirement_file_id,
            "project_name": "",
            "document_title": "",
            "document_name": self._resolve_original_name(requirement_file_id) or "",
            "document_structure": document_structure,
            "text_content": text_content,
            "table_summaries": table_summaries,
            "image_count": len(result.image_blocks),
            "image_texts": image_texts,
            # Per template-fidelity contract, this tool ONLY reports
            # what the document reader actually observed.  Higher-level
            # analysis (modules / features / roles / flows / rules /
            # exception scenarios) is the responsibility of the
            # template section package assembled by
            # TestPlanGeneratorTool from the template's table schemas
            # — the requirement parser must never invent structure the
            # template does not declare.
        }

        context.requirement_analysis = data

        return self._success(
            data,
            f"需求文档解析完成：识别 {len(document_structure)} 个文档章节、"
            f"{len(table_summaries)} 个表格、{len(result.image_blocks)} 张图片"
            + (
                f"、{sum(1 for r in image_results.values() if r.vision is not None and r.vision.ok)} 张图片理解成功"
                if image_results
                else ""
            ),
            warnings=warnings,
        )

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _build_text_content_with_images(
        result: DocumentReadResult,
        image_results: dict[int, ImageBlockResult],
    ) -> str:
        """Render the full document as prompt-ready text.

        Mirrors :meth:`DocumentReadResult.to_prompt_text` but uses
        the F020 :class:`ImageBlockResult` objects (which carry OCR
        and Vision output) instead of the legacy
        ``ImageUnderstandingResult`` placeholder contract.
        """
        lines: list[str] = []
        for block in result.blocks:
            if block.block_type == "text":
                if block.text:
                    lines.append(block.text)
            elif block.block_type == "table":
                if block.text:
                    lines.append(f"【表格 {block.index}】\n{block.text}")
            elif block.block_type == "image":
                image_result = image_results.get(block.index)
                if image_result is not None:
                    lines.append(image_result.to_prompt_text())
                elif block.image_path:
                    lines.append(
                        "\n".join(
                            [
                                f"【图片 {block.index}】",
                                f"来源位置：{block.source or '需求文档'}",
                                f"本地路径：{block.image_path}",
                                "处理状态：未参与 OCR/视觉处理",
                            ]
                        )
                    )
        return "\n\n".join(line for line in lines if line.strip())

    @staticmethod
    def _apply_text_length_policy(
        text: str,
        *,
        warning_threshold: int,
    ) -> tuple[str, list[str]]:
        """Retain the complete parsed source and emit only a size warning.

        Long-input reduction belongs to the auditable requirement-evidence
        pipeline, where every source span has a manifest entry.  This parser
        must never discard a suffix before that pipeline can see it.
        """
        if len(text) <= max(0, int(warning_threshold)):
            return text, []
        return text, [
            "需求文档较长，已保留完整解析正文；生成前将按完整覆盖清单分块提取。"
        ]

    @staticmethod
    async def _resolve_image_config(context: AgentContext) -> Any:
        """Resolve the per-user image-understanding provider, or ``None``.

        Returns ``None`` when the user has no row (or the cache lookup
        fails) — the orchestrator treats this as "OCR only".  Routed
        through :class:`SettingsService` (F021-revised) so the read
        path matches the primary-model "SettingsService → cache →
        loader" contract.
        """
        svc = getattr(context, "settings_service", None)
        if svc is None:
            return None
        user_internal_id = getattr(context, "user_internal_id", 0) or 0
        if user_internal_id <= 0:
            return None
        try:
            return await svc.build_image_understanding_provider(user_internal_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RequirementParserTool: 图片理解配置解析失败 | err=%s", exc,
            )
            return None

    @staticmethod
    def _resolve_original_name(file_id: str) -> str:
        """Resolve the user-visible original filename for a file public_id.

        Returns ``""`` when the file row is missing / deleted / the DB lookup
        fails — the caller (narrative context builder) then falls back to the
        stored public_id instead of surfacing an internal id as display name.
        """
        try:
            from app.db.session import sync_engine
            from sqlalchemy import text

            with sync_engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT original_name FROM uploaded_files "
                        "WHERE public_id = :public_id AND deleted_at IS NULL "
                        "LIMIT 1"
                    ),
                    {"public_id": file_id},
                )
                row = result.fetchone()
                if row and row[0]:
                    return str(row[0])
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "original_name lookup for file %s failed: %s", file_id, exc
            )
        return ""

    @staticmethod
    def _resolve_storage(file_id: str) -> tuple[str | None, str]:
        """Resolve a file public_id to (storage_path, storage_type) in one DB round trip."""
        try:
            from app.db.session import sync_engine
            from sqlalchemy import text

            with sync_engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT storage_path, storage_type FROM uploaded_files "
                        "WHERE public_id = :public_id AND deleted_at IS NULL "
                        "LIMIT 1"
                    ),
                    {"public_id": file_id},
                ).fetchone()
                if row and row[0]:
                    return str(row[0]), str(row[1] or "local")
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "DB lookup for file %s failed: %s", file_id, exc
            )
        return None, "local"

    @staticmethod
    def _build_document_structure(blocks) -> list[dict]:
        structure = []
        text_index = 0
        for block in blocks:
            if block.block_type == "text":
                text_index += 1
                summary = block.text[:120].replace("\n", " ")
                structure.append({
                    "heading_id": f"h_{text_index:03d}",
                    "title": summary[:60] + ("..." if len(summary) > 60 else ""),
                    "level": 0,
                    "text_summary": summary,
                })
            elif block.block_type == "table":
                structure.append({
                    "heading_id": f"tbl_{block.index:03d}",
                    "title": f"表格 {block.index}",
                    "level": 0,
                    "text_summary": block.text[:120],
                })
            elif block.block_type == "image":
                structure.append({
                    "heading_id": f"img_{block.index:03d}",
                    "title": f"图片 {block.index}",
                    "level": 0,
                    "text_summary": f"[图片] {block.source}",
                })
        return structure

    @staticmethod
    def _build_table_summaries(blocks) -> list[dict]:
        return [
            {
                "table_index": block.index,
                "summary": block.text[:200],
            }
            for block in blocks
            if block.block_type == "table"
        ]

    @staticmethod
    async def _run_in_thread(func, *args):
        import asyncio
        return await asyncio.to_thread(func, *args)
