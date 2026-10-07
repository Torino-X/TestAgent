"""DocxFormatCheckTool — compares the exported docx against the template.

F025 — runs after ``WordExportTool`` in the post-confirm phase.  This
tool does NOT modify anything; it just reports structural drift.

F025-ext — also detects user-visible *losses* (bookmarks, fields,
hyperlinks, headers, footers) and surfaces them as a
``loss_details_for_user`` payload.

Phase 2.9A.17: 错误码分层为 6 个互斥语义:
  * ``FORMAT_NO_ARTIFACT``     — Graph State 与 Repository 双查都没有 Artifact
  * ``FORMAT_ARTIFACT_INVALID`` — Artifact 结构不合法(非 dict / 字段全空)
  * ``FORMAT_ARTIFACT_PATH_MISSING`` — Artifact 有值但 storage_path 为空
  * ``FORMAT_ARTIFACT_FILE_NOT_FOUND`` — 路径解析成功但物理文件不存在
  * ``FORMAT_ARTIFACT_TYPE_UNSUPPORTED`` — Artifact 类型不是受支持的 docx
  * ``FORMAT_CHECK_EXECUTION_FAILED`` — 比较器自身抛异常

Phase 2.9A.20 (OSS 化): 增加 ``FORMAT_TEMPLATE_FILE_NOT_FOUND``,并让
``_absolutise`` 自动识别 OSS 不透明 object key,使用
:func:`OSSFileStorageService.stage_file` 将其落地为临时文件后再比较。

Reads 顺序(显式 fallback):
    inputs.artifact_path  → context.artifact["storage_path"]  →
    inputs.artifact["storage_path"]

Phase 2.9A.17:每次成功运行都会回写 ``checked_artifact_public_id`` 到 Graph
State(供后置 finalize_task / downstream SSE payload 引用)。

════════════════════════════════════════════════════════════════════════════
链路位置 (Phase 2.1 起就被 8-Tool 链路调用):

  节点 check_docx_format_node
    → DocxFormatCheckTool.run(inputs={"artifact_path": ...})
      → 6 个错误码之一 → 返回 _error
      → 成功:走 app.common.format_checker + app.common.word_exporter 比对模板与产物
      → 把 level(passed/warning/blocked/failed) + losses 写入 state.format_check_result

调用合约(供开发者速查):
  - 检查失败(blocked / failed)→ route_after_format_check 决定下一节点:
    通过 → finalize_task;blocked 走 legacy / 真 interrupt 让用户决策;
  - 路由写入 state.checked_artifact_public_id 让 finalize_task / downstream
    SSE payload 引用;
  - 二次结构丢失(secondary features: bookmarks/annotations/footnotes/endnotes)
    → 触发 export_word_node 把 pending_format_losses 写入,挂 format_loss_interrupt。
════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, Optional

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.common.format_checker import compare_docx_to_template
from app.core.exceptions import NotFoundError
from app.storage.local_storage import local_storage
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


# Phase 2.9A.17 §5.6:6 个互斥错误码
FORMAT_NO_ARTIFACT = "FORMAT_NO_ARTIFACT"
FORMAT_ARTIFACT_INVALID = "FORMAT_ARTIFACT_INVALID"
FORMAT_ARTIFACT_PATH_MISSING = "FORMAT_ARTIFACT_PATH_MISSING"
FORMAT_ARTIFACT_FILE_NOT_FOUND = "FORMAT_ARTIFACT_FILE_NOT_FOUND"
FORMAT_ARTIFACT_TYPE_UNSUPPORTED = "FORMAT_ARTIFACT_TYPE_UNSUPPORTED"
FORMAT_CHECK_EXECUTION_FAILED = "FORMAT_CHECK_EXECUTION_FAILED"
# Phase 2.9A.20: OSS 化后,templates 与 artifacts 一样是 OSS key。
# 当 ``_absolutise`` 判定路径是 OSS key 但对象不存在时,使用单独的
# template 错误码以便上层路由决策。
FORMAT_TEMPLATE_FILE_NOT_FOUND = "FORMAT_TEMPLATE_FILE_NOT_FOUND"
FORMAT_ARTIFACT_MISSING_PUBLIC_ID = "FORMAT_ARTIFACT_MISSING_PUBLIC_ID"
# 兼容旧路径
FORMAT_NO_TEMPLATE = "FORMAT_NO_TEMPLATE"
FORMAT_FILE_MISSING = "FORMAT_FILE_MISSING"
FORMAT_CHECK_FAILED = "FORMAT_CHECK_FAILED"


class DocxFormatCheckTool(BaseTool):
    name = "DocxFormatCheckTool"
    description = (
        "比较导出的 docx 与原始模板的结构差异（标题层级、表格数、列数，"
        "并检测书签 / 字段 / 超链接 / 页眉页脚 等用户可见丢失）"
    )

    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: RetryContext | None = None,
    ) -> dict:
        # Phase 2.9A.17: 先看 inputs.artifact_path → context.artifact.storage_path → inputs.artifact.storage_path
        # 三级 fallback,但任一命中后都做结构校验,失败码单独定
        artifact_path = inputs.get("artifact_path")
        if not artifact_path:
            artifact_path = self._artifact_storage_path(context)
        if not artifact_path and isinstance(inputs.get("artifact"), dict):
            artifact_path = inputs["artifact"].get("storage_path")

        if not artifact_path:
            # Phase 2.9A.17: 复用 FORMAT_NO_ARTIFACT 时同时检查 artifact 字段存在性,
            # 若连结构都不存在则是 _INVALID;有 dict 但缺路径则是 _PATH_MISSING。
            artifact_struct = inputs.get("artifact") or getattr(context, "artifact", None)
            if not isinstance(artifact_struct, dict) or not artifact_struct:
                return self._error(
                    FORMAT_NO_ARTIFACT,
                    "DocxFormatCheckTool: 缺失 artifact_path 且 Graph State 无 Artifact",
                    recoverable=False,
                )
            return self._error(
                FORMAT_ARTIFACT_PATH_MISSING,
                "DocxFormatCheckTool: Artifact 结构存在但 storage_path 为空",
                recoverable=False,
            )

        # 类型校验
        artifact_struct = inputs.get("artifact") or getattr(context, "artifact", None) or {}
        if isinstance(artifact_struct, dict):
            artifact_type = artifact_struct.get("artifact_type")
            if artifact_type and artifact_type != "test_plan_word":
                return self._error(
                    FORMAT_ARTIFACT_TYPE_UNSUPPORTED,
                    f"DocxFormatCheckTool: Artifact 类型 {artifact_type!r} 不受支持",
                    recoverable=False,
                )

        template_path = inputs.get("template_path") or self._template_storage_path(context)
        if not template_path:
            # 用旧的 FORMAT_NO_TEMPLATE 保留向后兼容
            return self._error(
                FORMAT_NO_TEMPLATE,
                "DocxFormatCheckTool: 缺少 template_path，且 ctx.template_file_id 无法解析",
                recoverable=False,
            )

        # Phase 2.9A.20: OSS 化后 artifact / template 的 storage_path 是
        # 不透明 object key。``_absolutise`` 会自动检测 OSS key,使用
        # ``stage_file`` 落地为临时文件,作用域与本次工具调用一致。
        staged: ExitStack = ExitStack()
        try:
            try:
                abs_artifact = self._absolutise(artifact_path, staged=staged, role="artifact")
            except FileNotFoundError as exc:
                logger.warning(
                    "DocxFormatCheckTool: artifact 文件缺失 | path=%s | err=%s",
                    artifact_path, exc,
                )
                return self._error(
                    FORMAT_ARTIFACT_FILE_NOT_FOUND,
                    f"DocxFormatCheckTool: artifact 物理文件不存在 ({artifact_path})",
                    recoverable=False,
                )

            try:
                abs_template = self._absolutise(template_path, staged=staged, role="template")
            except FileNotFoundError as exc:
                logger.warning(
                    "DocxFormatCheckTool: template 文件缺失 | path=%s | err=%s",
                    template_path, exc,
                )
                return self._error(
                    FORMAT_TEMPLATE_FILE_NOT_FOUND,
                    f"DocxFormatCheckTool: template 物理文件不存在 ({template_path})",
                    recoverable=False,
                )

            # ── Run comparison
            try:
                report = await asyncio.to_thread(
                    compare_docx_to_template,
                    abs_template,
                    abs_artifact,
                )
            except FileNotFoundError as exc:
                logger.warning(
                    "DocxFormatCheckTool: 比较器报告文件不存在 | artifact=%s | template=%s | err=%s",
                    abs_artifact, abs_template, exc,
                )
                return self._error(
                    FORMAT_ARTIFACT_FILE_NOT_FOUND,
                    f"DocxFormatCheckTool: {exc}",
                    recoverable=False,
                )
            except Exception as exc:
                logger.exception("DocxFormatCheckTool: 比较异常")
                return self._error(
                    FORMAT_CHECK_EXECUTION_FAILED,
                    f"格式比较异常:{exc}",
                    recoverable=True,
                )
        finally:
            staged.close()

        payload = report.to_dict()
        # 真实物理路径是临时文件,在 response 中只回写对调用方有意义的原始
        # key,避免泄露 staging 临时路径。``checked_artifact_public_id`` 已经
        # 能稳定定位 artifact。
        payload["artifact_path"] = artifact_path
        payload["template_path"] = template_path
        # Phase 2.9A.17 §4.6: 绑定 checked_artifact_public_id
        if isinstance(artifact_struct, dict):
            payload["checked_artifact_public_id"] = artifact_struct.get(
                "public_id"
            ) or artifact_struct.get("artifact_id")
            if not payload["checked_artifact_public_id"]:
                logger.warning(
                    "DocxFormatCheckTool: artifact 结构缺 public_id/artifact_id | keys=%s",
                    sorted(artifact_struct.keys()),
                )

        level = report.level
        msg = {
            "passed": "导出文档结构与模板一致",
            "warning": f"导出文档结构与模板有 {len(report.drifts)} 处轻微差异",
            "failed": (
                f"导出文档结构与模板严重不符({len(report.drifts)} 处漂移)"
            ),
            "loss_detected": (
                f"检测到 {len(report.losses)} 项用户可见的格式丢失"
            ),
        }.get(level, "格式检查完成")

        logger.info(
            "DocxFormatCheckTool: level=%s | drift=%d | losses=%d | "
            "headings=%s | tables=%d | checked_artifact_public_id=%s",
            level, len(report.drifts), len(report.losses),
            payload["headings"], report.table_count,
            payload.get("checked_artifact_public_id"),
        )

        return self._success(payload, msg)

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _looks_like_oss_key(path: str) -> bool:
        """Heuristic: an opaque object key under our configured prefix.

        Returns True when ``path`` is a forward-slash separated string that
        starts with the OSS prefix (default ``testagent``) and is not an
        absolute filesystem path.  Used to disambiguate between legacy
        local-relative storage paths and modern OSS keys.
        """
        if not path or os.path.isabs(path):
            return False
        if "\\" in path:
            return False
        from app.storage.oss_storage import object_storage

        prefix = object_storage._prefix().strip("/ ")
        if prefix and not path.startswith(f"{prefix}/"):
            return False
        return True

    @classmethod
    def _absolutise(
        cls,
        storage_path: str | Path,
        *,
        staged: ExitStack,
        role: str,
    ) -> Path:
        """Resolve a stored path to a local absolute ``Path``.

        ``staged`` is an :class:`ExitStack` shared by the call.  When the
        path is detected as an OSS object key the file is staged onto a
        ``tempfile.NamedTemporaryFile`` and the cleanup is registered on
        ``staged``; this keeps the bytes alive for the duration of the
        comparison.  Legacy relative paths still resolve against
        ``local_storage._base`` for backward compatibility with rows that
        predate the OSS migration.
        """
        raw = str(storage_path)
        if cls._looks_like_oss_key(raw):
            from app.storage.oss_storage import object_storage

            # Stage synchronously into a tempfile.  The ExitStack ensures
            # the temporary file is removed when the tool call returns.
            content = object_storage.read_bytes_sync(raw)
            suffix = Path(raw).suffix or ".docx"
            fd, name = tempfile.mkstemp(prefix=f"docx_check_{role}_", suffix=suffix)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            staged.callback(_safe_unlink, name)
            return Path(name)
        p = Path(raw)
        if p.is_absolute():
            return p
        return local_storage._base / p

    @staticmethod
    def _artifact_storage_path(context: AgentContext) -> str | None:
        artifact = getattr(context, "artifact", None) or {}
        if not isinstance(artifact, dict):
            return None
        return artifact.get("storage_path")

    @staticmethod
    def _template_storage_path(context: AgentContext) -> str | None:
        template_file_id = getattr(context, "template_file_id", None)
        if not template_file_id:
            return None
        try:
            from app.tools.template_parser_tool import TemplateParserTool
            return TemplateParserTool._resolve_storage_path(template_file_id)
        except Exception as exc:
            logger.warning(
                "DocxFormatCheckTool: 无法解析 template_file_id=%s | err=%s",
                template_file_id, exc,
            )
            return None


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - best effort cleanup
        logger.warning("DocxFormatCheckTool: 无法清理 staging 临时文件 %s | err=%s", path, exc)
