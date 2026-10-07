"""WordExportTool — exports the generated test plan as .docx.

Reads ``context.test_plan_content.section_package`` (built by
TestPlanGeneratorTool → ResultParser.build_section_package()),
resolves the original template file, and performs high-fidelity
template backfill via ``WordExporter``.

All 34 capabilities from the legacy pipeline preserved except
Word COM automation (server-environment incompatible).

Output: standard Tool result with artifact_id + download_url.

════════════════════════════════════════════════════════════════════════════════
链路位置 (Phase 2.1 起就被 8-Tool 链路调用,产物落地核心):

  节点 export_word_node
    → WordExportTool.run(inputs={"template_file_id": ...,
                                 "fidelity": "high"|"medium"|"low",
                                 "format_loss_review": bool})
      → app.storage.file_reference_resolver 解析模板文件
      → app.common.word_exporter.WordExporter.export_from_template(...)
      → 写入 ctx.artifact = {public_id, file_name, file_size, storage_path, ...}
      → 失败/格式损失发 error_code + format_loss_pending

调用合约(供开发者速查):
  - 失败错误码:EXPORT_CONTENT_MISSING / EXPORT_TEMPLATE_NOT_FOUND /
    EXPORT_TEMPLATE_INVALID / EXPORT_ARTIFACT_MISSING_PUBLIC_ID /
    EXPORT_WRITE_FAILED / EXPORT_TEMPLATE_BACKFILL_INCOMPATIBLE;
  - fidelity 由 format_loop_count 决定(失败重试时降低保真度);
  - Phase 2.9A.X 后:detect 到 secondary 结构丢失(书签/批注/脚注/尾注)
    → 不再 task_failed,而是把 pending_format_losses 写入后挂
    format_loss_interrupt 让用户决策 accept/retry/reject;
  - WordExporter.pending_format_losses 在 export 阶段保留着 secondary 损失
    列表,供 export_word_node 透传。

辅助函数 _extract_project_name 在最近修复中加了三段兜底:
模板 basename(去 .docx,需不像模板文件名) → task_id 短前缀(取 task_ 后 8 字符)
→ "未命名项目";LLM 推断整体包了 asyncio.wait_for(timeout=3.0)。

输出经过 narrative_composer DocxExportContextBuilder 包装,生成"导出完成"叙事。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.common.word_exporter import WordExporter, WordExportError
from app.context_engine.feature_flags import require_agent_context_migration
from app.fault_injection_gateway import (
    POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER,
    maybe_inject_fault,
)
from app.storage.file_reference_resolver import (
    FileRefFailure,
    ResolvedFileReference,
    resolve_file_reference_async,
    resolve_file_reference_sync,
    resolve_template_for_export_async,
    resolve_template_for_export_sync,
)
from app.storage.oss_storage import object_storage
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


# BUG FIX 2026-08-19：项目名 LLM 推断的超时常量。原值 2s 对 DeepSeek 过紧，
# 主模型调用超时后虽走 fallback 不阻塞导出，但产生 ERROR 日志污染。
# 统一改为 20s（内层 timeout_override） + 30s（外层 asyncio.wait_for），
# 与 agent_runtime 其他 LLM 调用对齐。
_PROJECT_TITLE_LLM_TIMEOUT_OVERRIDE_S = 20
_PROJECT_TITLE_TOTAL_BUDGET_S = 30.0


class WordExportTool(BaseTool):
    name = "WordExportTool"
    description = "将生成的测试方案内容导出为 Word 文档"

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        from app.capabilities.artifact_policy import unsupported_format_operations

        unsupported_ops = unsupported_format_operations(inputs)
        if unsupported_ops:
            return self._error(
                "EXPORT_UNSUPPORTED_FORMAT_OPERATION",
                "当前版本仅支持基于模板的内容回填，不支持任意 Word 表格或版式格式修改。"
                "可修改章节正文后重新按模板导出。",
                recoverable=False,
                details={"unsupported_operations": unsupported_ops},
            )
        # ── 1. Read section_package from context ──────────────────────
        test_plan_content = context.test_plan_content
        if not test_plan_content or not isinstance(test_plan_content, dict):
            return self._error(
                "EXPORT_CONTENT_MISSING",
                "缺少测试方案生成内容，请先生成测试方案",
                recoverable=False,
            )

        section_package = test_plan_content.get("section_package")
        if not section_package or not isinstance(section_package, dict):
            # Fallback: test_plan_content itself might be the section_package
            if "generated_sections" in test_plan_content:
                section_package = test_plan_content
            else:
                return self._error(
                    "EXPORT_CONTENT_MISSING",
                    "缺少 section_package，无法进行高保真导出",
                    recoverable=False,
                )

        generated_sections = section_package.get("generated_sections", [])
        if not generated_sections:
            return self._error(
                "EXPORT_CONTENT_MISSING",
                "section_package 中没有 AI 生成章节，无法导出",
                recoverable=False,
            )

        # Check for body_start_index (required for template backfill)
        has_index = any(
            isinstance(s, dict) and s.get("body_start_index") is not None
            for s in generated_sections
        )
        logger.warning(
            "WordExportTool: 内容检查 | generated_sections=%d | has_body_start_index=%s",
            len(generated_sections), has_index,
        )

        # ── 2. Resolve template file ─────────────────────────────────
        template_file_id = (
            inputs.get("template_file_id") or context.template_file_id
        )

        # Phase 2.9A.13: 统一通过 Resolver 解析,而不是工具内自己拼 SQL。
        # 支持 public_id 与内部 ID 两种形态(历史 Graph State 可能混入)。
        resolved: ResolvedFileReference
        if context.session is not None:
            resolved = await resolve_template_for_export_async(
                session=context.session,
                template_file_id=template_file_id,
                user_internal_id=context.user_internal_id,
                conversation_internal_id=context.conversation_internal_id,
                task_internal_id=context.task_internal_id,
            )
        else:
            resolved = resolve_template_for_export_sync(
                template_file_id=template_file_id,
                user_internal_id=context.user_internal_id,
                conversation_internal_id=context.conversation_internal_id,
                task_internal_id=context.task_internal_id,
            )

        # 诊断日志:覆盖 16 个字段,绝不含绝对路径
        log_payload = dict(resolved.to_log_dict())
        log_payload["template_ref_present"] = bool(template_file_id)
        log_payload["content_section_count"] = len(generated_sections)
        log_payload["content_length"] = sum(
            len(str((s or {}).get("content") or "")) for s in generated_sections
        )
        logger.warning(
            "WordExportTool: 模板解析 | %s",
            " ".join(f"{k}={v}" for k, v in log_payload.items()),
        )

        if not resolved.exists or resolved.absolute_path == "":
            # 用户错误码保持 EXPORT_TEMPLATE_NOT_FOUND(向后兼容);
            # 内部 metadata 通过 details 区分失败阶段。
            return self._error(
                "EXPORT_TEMPLATE_NOT_FOUND",
                f"未找到测试方案模板文件（{resolved.failure_stage or FileRefFailure.RECORD_NOT_FOUND}）",
                recoverable=(
                    resolved.failure_stage
                    in (
                        FileRefFailure.PATH_RESOLUTION_FAILED,
                        FileRefFailure.PHYSICAL_FILE_NOT_FOUND,
                    )
                ),
                details={
                    "stage": resolved.failure_stage,
                    "role_match": resolved.role_match,
                    "ref_kind": resolved.failure_detail,
                },
            )

        template_path = resolved.absolute_path

        # ── 3. Determine output filename ─────────────────────────────
        project_name = await self._extract_project_name(
            context,
            Path(template_path).name if template_path else None,
        )
        file_name = inputs.get(
            "file_name", f"{project_name}_测试方案.docx"
        )

        # ── 4. Perform export via WordExporter (write to tmp, atomic rename) ─────
        # Phase 2.8R-E: 出口文件先写到 tmp,atomic rename 到 final ——
        # 防止中途崩溃留下半成品 Word 文件。
        font_family = inputs.get("font_family", "宋体")
        fidelity = inputs.get("fidelity", "high")
        exporter = WordExporter(
            font_family=font_family, fidelity=fidelity
        )

        # Resolve output path via local_storage
        output_path: str | None = None
        export_start = time.monotonic()
        tmp_output_path: str | None = None
        try:
            # Build a safe artifact storage path
            safe_name = self._safe_filename(file_name)
            descriptor, output_path = tempfile.mkstemp(suffix=f"_{safe_name}")
            os.close(descriptor)
            # Tmp path:同目录以保证 rename 原子性(uuid 避免并发冲突)
            tmp_output_path = f"{output_path}.tmp.{uuid.uuid4().hex[:8]}"

            logger.info(
                "WordExportTool: 开始导出 | 输出路径=%s | 模板=%s",
                output_path, Path(template_path).name,
            )

            # Blocking docx I/O → run in thread;写 tmp 文件
            await asyncio.to_thread(
                exporter.export_from_template,
                template_path,
                section_package,
                tmp_output_path,
            )

            maybe_inject_fault(
                POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER,
                exporter,
                context={
                    "task_id": getattr(context, "task_id", None)
                    or getattr(context, "task_internal_id", None),
                    "tool": self.name,
                },
            )

            # Atomic rename(tmp → final)— 2.8R-E 防止半成品文件
            os.replace(tmp_output_path, output_path)
            tmp_output_path = None  # 已 rename,不需要清理

        except WordExportError as exc:
            logger.exception("WordExportTool: export failed")
            resolved.cleanup_staged_file()
            return self._error("EXPORT_FAILED", str(exc), recoverable=True)
        except Exception as exc:
            logger.exception("WordExportTool: unexpected error")
            # 清理残留 tmp
            if tmp_output_path is not None:
                try:
                    if os.path.exists(tmp_output_path):
                        os.unlink(tmp_output_path)
                except OSError:
                    pass
            resolved.cleanup_staged_file()
            return self._error(
                "EXPORT_FAILED",
                f"Word 导出失败：{exc}",
                recoverable=True,
            )

        # ── 5. Gather file metadata ──────────────────────────────────
        artifact_content = await asyncio.to_thread(Path(output_path).read_bytes)
        file_size = len(artifact_content)
        page_count = await asyncio.to_thread(self._read_docx_page_count, output_path)
        storage_path: str | None = None
        try:
            stored = await object_storage.save_artifact(
                artifact_content,
                file_name,
                str(context.user_internal_id or context.user_id),
                str(context.task_internal_id or context.task_id),
            )
            storage_path = stored.storage_path
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                logger.warning("WordExportTool: unable to remove temporary export file")
        logger.info(
            "WordExportTool: 导出成功 | 文件大小=%d字节 | 耗时=%.1fs",
            file_size, time.monotonic() - export_start,
        )

        # ── 6. Integrity result ──────────────────────────────────────
        integrity = self._build_integrity_result(exporter)

        # ── 7. Create artifact record (if DB session available) ──────
        artifact_public_id: str | None = None
        if context.session is not None:
            artifact_public_id = await self._create_artifact_record(
                context=context,
                file_name=file_name,
                file_ext="docx",
                file_size=file_size,
                storage_path=storage_path,
                file_content=artifact_content,
                metadata={
                    "integrity": integrity,
                    "template_file_id": template_file_id,
                    "sections_count": len(generated_sections),
                    "page_count": page_count,
                    "test_plan_content": context.test_plan_content or {},
                    "template_structure": context.template_structure or {},
                    "review_standard": context.review_standard or {},
                    "review_result": context.review_result or {},
                    "requirement_analysis": context.requirement_analysis or {},
                    "section_confirm_config": context.section_confirm_config or {},
                },
            )
        else:
            # Generate a public_id even without DB
            from app.utils.ids import generate_public_id
            artifact_public_id = generate_public_id("artifact")
            logger.info(
                "WordExportTool: no DB session — artifact not persisted. "
                "public_id=%s", artifact_public_id
            )

        # ── 8. Build Tool output ────────────────────────────────────
        # Phase 2.9A.16: 显式返回完整 Artifact 到 data 中。
        # Graph v3 不再依赖 context.proxied side-effect 传递 storage_path。
        # 但 data 仍只包含相对 storage_path（不包含绝对路径），
        # 避免泄露磁盘布局。
        data: Dict[str, Any] = {
            "artifact_id": artifact_public_id,
            "artifact_type": "test_plan_word",
            "file_name": file_name,
            "file_ext": ".docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size": file_size,
            "storage_type": object_storage.storage_type,
            "page_count": page_count,
            "storage_path": storage_path,
            "download_url": f"/api/v1/artifacts/{artifact_public_id}/download",
            "integrity": integrity,
        }
        # Phase 2.9A.X: secondary 结构丢失(书签 / 批注 / 脚注 / 尾注)记入
        # ``format_loss_pending`` + ``pending_format_losses``,由 export_word_node
        # 透传到 ``state``,走 ``format_loss_review`` 让用户决定继续 / 重试 / 放弃。
        # 即使 medium fidelity 已容忍,用户仍应被告知文件结构有问题。
        if getattr(exporter, "pending_format_losses", None):
            data["format_loss_pending"] = True
            data["pending_format_losses"] = list(exporter.pending_format_losses)
            logger.info(
                "WordExportTool: secondary 结构丢失,等待用户确认 | "
                "losses=%s",
                exporter.pending_format_losses,
            )

        # Write artifact reference to context.
        #
        # 2026-07：上一版本直接 `context.artifact = data`，结果
        # DocxFormatCheckTool._artifact_storage_path() 读
        # ``artifact.get("storage_path")`` 拿到 None，触发
        # FORMAT_NO_ARTIFACT（虽然文档实际已写入磁盘 + DB）。修复：
        # ``storage_path`` 只进 ctx（供下游工具消费），不进 ``data``
        # （API 响应——这条合约见 line 199 的注释）。
        context_artifact = {**data, "storage_path": storage_path}
        context.artifact = context_artifact

        # Build summary
        integrity_level = integrity.get("level", "unknown")
        warnings = list(exporter.warnings)
        summary_parts = [
            f"测试方案 Word 文档已导出：{file_name}",
            f"（{self._readable_size(file_size)}）",
        ]
        if integrity_level != "high":
            summary_parts.append(f"保真度：{integrity_level}")
        summary = " ".join(summary_parts)

        resolved.cleanup_staged_file()
        return self._success(data, summary, warnings=warnings)

    # ── Internal helpers ──────────────────────────────────────────

    @staticmethod
    def _resolve_template_path(file_id: str) -> str | None:
        """Deprecated — use :func:`resolve_template_for_export_sync` instead.

        Kept for backward compatibility with older unit tests that called
        this method directly.  Returns the absolute path string when the
        file resolves successfully, otherwise ``None``.  The new
        :class:`ResolvedFileReference` carries richer failure metadata;
        see ``app/storage/file_reference_resolver.py``.
        """
        resolved = resolve_template_for_export_sync(file_id)
        return resolved.absolute_path or None

    @staticmethod
    async def _extract_project_name(
        context: AgentContext,
        template_basename: str | None = None,
    ) -> str:
        """Extract project name from requirement_analysis, LLM inference, template, or task_id.

        Priority:
          1. requirement_analysis.project_name (if non-empty)
          2. LLM inference from requirement text (best-effort, total budget ≤ 3s)
          3. Template basename (only if it doesn't look like a template filename)
          4. task_id

        BUG FIX 2026-08-18 (D): 用 asyncio.wait_for 把整个 LLM 推断包成
        3s 总预算,内层 timeout_override 2s。失败/超时走模板 basename
        → task_id 兜底,不再白等 10s+ 污染日志,Word 导出不被项目名阻塞。

        BUG FIX 2026-08-18 (C): 模板 basename 兜底**仅当 basename 不像
        "模板文件名"** 时才使用。用户上传模板时可能命名不规范
        (如 ``00_PlanWise_QA_测试方案模板.docx`` 带版本号 / 含"模板" /
        含日期哈希前缀),如果直接兜底用作项目名,会让产物文件名带
        这些不规范字符。判定特征:
          * 命中"测试方案"/"模板"/"template" 关键词
          * 含日期+哈希前缀(如 ``20260818_c25a1edf_``)
          * 含版本号前缀(如 ``v1_`` / ``v2_``)
        命中任一即跳过,fallback 到 task_id 兜底。
        """
        req = context.requirement_analysis or {}
        name = req.get("project_name", "")
        if name:
            return name

        text_content = req.get("text_content", "")
        if not text_content and context.user_prompt:
            text_content = context.user_prompt
        if text_content:
            try:
                inferred = await asyncio.wait_for(
                    WordExportTool._llm_infer_project_name(context, text_content[:2000]),
                    timeout=_PROJECT_TITLE_TOTAL_BUDGET_S,
                )
                if inferred:
                    return inferred
            except asyncio.TimeoutError:
                logger.warning(
                    "WordExportTool: LLM project_name inference timed out (>%ss), using fallback",
                    int(_PROJECT_TITLE_TOTAL_BUDGET_S),
                )
            except Exception as exc:  # noqa: BLE001 — LLM failure must not block export
                logger.warning(
                    "WordExportTool: LLM project_name inference failed | error=%s",
                    type(exc).__name__,
                )

        filename_project = _project_name_from_requirement_filename(
            req.get("document_name") or req.get("file_name") or req.get("original_name")
        )
        if filename_project:
            return filename_project

        # Fallback 链:模板 basename(仅当不像模板名)→ task_id 短前缀 → "未命名项目"。
        # best-effort 兜底,不静默报错,Word 导出不被项目名阻塞。
        # BUG FIX 2026-08-18: context.task_id 是完整 public_id
        # (如 "task_9ae68fed"),直接用作文件名既暴露内部 ID 又冗长。
        # 取 "task_" 后的 8 字符作短前缀(如 "task_9ae68fed" → "9ae68fed")。
        if template_basename and not _looks_like_template_name(template_basename):
            stem = (
                template_basename.rsplit(".", 1)[0]
                if "." in template_basename
                else template_basename
            )
            if stem:
                return stem
        if context.task_id:
            short = context.task_id
            if short.startswith("task_") and len(short) > 5:
                short = short[5:]  # 去掉 "task_" 前缀
            return short[:8]  # 取短前缀
        return "未命名项目"

    @staticmethod
    async def _llm_infer_project_name(context: AgentContext, snippet: str) -> str | None:
        """Inner helper:best-effort LLM inference。返回推断名或 None(失败/超时)。

        调用方负责用 asyncio.wait_for 包住以控制总耗时。本函数**不抛**
        TimeoutError(留给调用方 wait_for 触发),但仍可能抛其他异常
        (如 ImportError / AttributeError),由调用方 except 兜底。
        """
        prompt = (
            "从以下软件需求文档中提取一个简短的项目名称（2-6个中文字符）。"
            "只输出项目名称，不要输出其他任何内容。"
            "如果无法判断，输出空字符串。"
        )

        # CE-05 WP-2:任务路径经 context.task_flag_resolver 读 MIG_GENERATE
        resolver = getattr(context, "task_flag_resolver", None)
        migration_error = require_agent_context_migration(resolver, "MIG_GENERATE")
        if migration_error is None:
            # MIG=true → 只走 ContextInvokerBridge;失败即放弃推断,不阻塞导出
            from app.llm.task_profiles import LLMParserType, LLMTaskProfile
            from app.tools._mig_routing import invoke_via_bridge_or_none

            _profile = LLMTaskProfile(
                name="word_export.project_title.v1",
                system_prompt=prompt,
                parser=LLMParserType.PLAIN_TEXT,
                allow_markdown=False,
                require_json=False,
                max_tokens=64,
                temperature=0.0,
                timeout_override=_PROJECT_TITLE_LLM_TIMEOUT_OVERRIDE_S,
            )
            _err, _raw = await invoke_via_bridge_or_none(
                context=context,
                call_site="word_export.project_title",
                llm_task_profile=_profile,
                current_goal=snippet,
                output_contract="text",
            )
            if _err is not None or _raw is None:
                # 非关键路径:bridge 失败不阻塞导出,返回 None 让调用方走 fallback
                logger.warning(
                    "WordExportTool: MIG_GENERATE Invoker 不可用 | code=%s",
                    _err,
                )
                return None
            inferred = str(_raw).strip().strip('"').strip("'").strip("。")
            if inferred and len(inferred) <= 20 and "\n" not in inferred:
                logger.info("WordExportTool: LLM inferred project_name=%s", inferred)
                return inferred
            return None

        logger.info(
            "WordExportTool: skip optional title inference because CE is not frozen | code=%s",
            migration_error,
        )
        return None

    @staticmethod
    def _safe_filename(name: str) -> str:
        """Produce a filesystem-safe filename with timestamp."""
        from datetime import datetime
        import re
        import unicodedata

        base = unicodedata.normalize("NFKD", name or "document")
        base = re.sub(r"[^\w\-.]", "_", base, flags=re.UNICODE)
        base = re.sub(r"_+", "_", base).strip("_") or "document"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{base}"

    @staticmethod
    def _read_docx_page_count(path: str | None) -> int | None:
        if not path or os.name != "nt":
            return None
        word = None
        doc = None
        try:
            import importlib

            client = importlib.import_module("win32com.client")
            dispatch_ex = getattr(client, "DispatchEx", None)
            word = dispatch_ex("Word.Application") if callable(dispatch_ex) else client.Dispatch("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(
                str(Path(path).resolve()),
                ReadOnly=True,
                AddToRecentFiles=False,
                Visible=False,
            )
            pages = int(doc.ComputeStatistics(2))  # wdStatisticPages
            return pages if pages > 0 else None
        except Exception as exc:  # noqa: BLE001 - optional local capability
            logger.info(
                "WordExportTool: Word page count unavailable | error=%s",
                type(exc).__name__,
            )
            return None
        finally:
            if doc is not None:
                try:
                    doc.Close(False)
                except Exception:
                    pass
            if word is not None:
                try:
                    word.Quit()
                except Exception:
                    pass

    @staticmethod
    def _build_integrity_result(exporter: WordExporter) -> dict:
        """Build an integrity summary from the exporter's warnings."""
        level = "high"
        if exporter.warnings:
            # If we have critical structure loss warnings, degrade
            critical_keywords = ["分节", "表格", "绘图", "控件", "对象"]
            has_critical = any(
                any(kw in w for kw in critical_keywords)
                for w in exporter.warnings
            )
            if has_critical:
                level = "low"
            else:
                level = "medium"

        return {
            "level": level,
            "warnings": list(exporter.warnings),
        }

    async def _create_artifact_record(
        self,
        context: AgentContext,
        file_name: str,
        file_ext: str,
        file_size: int,
        storage_path: str,
        file_content: bytes,
        metadata: dict | None = None,
    ) -> str:
        """Persist an Artifact record to the database.

        Phase 2.8R-E: 加 idempotency_key + input_hash(防止双进程并发产出
        同一 WordDoc 时双写)。优先 SELECT existing,再 INSERT。

        Returns the public_id for the API response. storage_path is
        written to the DB but NEVER returned to the caller.
        """
        from app.models.artifact import Artifact
        from app.repositories.artifact_repository import ArtifactRepository
        from app.services.artifact_writer import (
            compute_artifact_idempotency_key,
            compute_input_hash,
            resolve_task_project_id,
        )
        from app.utils.ids import generate_public_id
        from app.utils.datetime import utcnow
        public_id = generate_public_id("artifact")
        now = utcnow()
        project_id = await resolve_task_project_id(context.session, context.task_internal_id)

        # Compute idempotency_key — Phase 2.8R-E
        # input_hash 基于文件内容(读磁盘 sha256);idempotency_key 拼
        # task_public_id|test_plan_word|input_hash|task graph_run_id
        input_hash = compute_input_hash(file_content)
        task_public_id = context.task_id or ""
        graph_run_id = getattr(context, "graph_run_id", None)
        ikey = compute_artifact_idempotency_key(
            task_public_id=task_public_id,
            artifact_type="test_plan_word",
            input_hash=input_hash,
            graph_run_id=graph_run_id,
        )

        artifact = Artifact(
            public_id=public_id,
            user_id=context.user_internal_id,
            conversation_id=context.conversation_internal_id,
            task_id=context.task_internal_id,
            project_id=project_id,
            artifact_type="test_plan_word",
            file_name=file_name,
            file_ext=file_ext.lstrip("."),
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_size=file_size,
            file_hash=None,
            storage_type=object_storage.storage_type,
            storage_path=storage_path,
            status="available",
            version_no=1,
            metadata_json=metadata,
            idempotency_key=ikey,
            input_hash=input_hash,
            graph_run_id=graph_run_id,
            graph_version=getattr(context, "graph_version", None),
            created_at=now,
            updated_at=now,
        )

        repo = ArtifactRepository(context.session)
        # Phase 2.8R-E: 幂等创建 — UNIQUE 冲突则返回 existing 行
        row, created = await repo.create_or_get_by_idempotency_key(
            artifact, idempotency_key=ikey
        )
        if not created:
            await object_storage.delete_file(storage_path)
        public_id = row.public_id
        logger.info(
            "WordExportTool: artifact persisted — public_id=%s size=%d idemp_key=%s",
            public_id,
            file_size,
            ikey[:32],
        )
        # A completed Word output is a durable, versioned source.  Submit it
        # to the same asynchronous RAG pipeline as uploads, without blocking
        # a successful export if optional indexing infrastructure is degraded.
        try:
            from app.context_engine.indexing.document_service import (
                DocumentIndexError,
                IndexDocumentService,
            )

            result = await IndexDocumentService(context.session).submit_artifact(
                user_id=context.user_internal_id,
                artifact_public_id=public_id,
            )
            logger.info(
                "WordExportTool: artifact index submitted | artifact=%s document=%s status=%s",
                public_id,
                result.get("document_public_id"),
                result.get("status"),
            )
        except DocumentIndexError as exc:
            logger.warning(
                "WordExportTool: artifact index skipped | artifact=%s code=%s",
                public_id,
                exc.code,
            )
        except Exception as exc:  # noqa: BLE001 - export remains authoritative
            logger.warning(
                "WordExportTool: artifact index degraded | artifact=%s error=%s",
                public_id,
                type(exc).__name__,
            )
        return public_id

    @staticmethod
    def _readable_size(size_bytes: int) -> str:
        """Human-readable file size string."""
        for unit in ("B", "KB", "MB", "GB"):
            if size_bytes < 1024:
                return f"{size_bytes} {unit}"
            size_bytes //= 1024
        return f"{size_bytes} TB"


def _looks_like_template_name(name: str) -> bool:
    """判定 ``name`` 是否像"模板文件名"而非"项目名"。

    BUG FIX 2026-08-18 (C): 用户上传模板时命名常常不规范,常见特征:

      * 命中关键词:"测试方案" / "模板" / "template"(模板常见词)
      * 含日期+哈希前缀:``\\d{8}_[a-f0-9]{6,}_``(local_storage.safe_filename 风格)
      * 含版本号前缀:``v\\d+_`` / ``V\\d+_``(用户给模板加版本号)

    任一特征命中 → True(像模板文件名,跳过用作项目名)。

    Args:
        name: 模板文件 basename(可能含 .docx 后缀)

    Returns:
        True 表示该 name 像模板文件名,不应作为项目名兜底。
    """
    if not name or not isinstance(name, str):
        return True  # 空值默认保守当作模板名,走 task_id 兜底
    stem = name.rsplit(".", 1)[0] if "." in name else name
    lower = stem.lower()
    if any(kw in stem for kw in ("测试方案", "模板")) or "template" in lower:
        return True
    if re.match(r"^\d{8}_[a-f0-9]{4,}_", lower):
        return True
    if re.match(r"^v\d+_", lower):
        return True
    return False


def _project_name_from_requirement_filename(name: Any) -> str | None:
    """Infer project name from the original requirement filename.

    Example:
      ``01_智慧校园预约与签到系统_需求说明书.docx`` → ``智慧校园预约与签到系统``
    """
    if not isinstance(name, str) or not name.strip():
        return None
    stem = Path(name.strip()).name
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = re.sub(r"^\d{8}_[a-f0-9]{4,}_", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"^\d+[_\-\s]+", "", stem)
    stem = re.sub(r"^(?:PRD|需求文档|需求)[_\-\s]+", "", stem, flags=re.IGNORECASE)
    stem = re.sub(
        r"[_\-\s]*(?:PRD需求文档|需求规格说明书|需求说明书|需求文档|需求说明|PRD|需求)$",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    stem = re.sub(r"[_\-\s]+", "_", stem).strip("_ ").strip()
    if not stem or stem in {"需求", "需求文档", "需求说明书", "PRD"}:
        return None
    return stem[:40]
