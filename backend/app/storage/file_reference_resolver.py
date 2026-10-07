"""File reference resolver — single source of truth for file lookups.

Phase 2.9A.13 (Word 导出模板解析修复):

Graph State 中的 ``template_file_id`` 在历史上被不同入口混入
``public_id`` 字符串 (``file_xxx``) 和内部 ID (整数) 两种值,而
下游工具(TemplateParser / WordExport)各自实现了一套
``SELECT storage_path FROM uploaded_files WHERE public_id = ?`` 查询。
当 Graph State 写入了内部 ID,WordExport 的查询就拿不到 storage_path,
直接走到 fallback rglob,甚至连 rglob 也匹配失败 → 报
``EXPORT_TEMPLATE_NOT_FOUND``,尽管 TemplateParserTool 几乎同时
用相同输入文件成功解析。

本模块:

- 统一接受 ``public_id`` (e.g. ``file_003ccd34``) 与内部 ID 字符串
  (e.g. ``"112"`` 或 ``"file_112"``) 两种形态;
- 通过 SQLAlchemy 走 ``uploaded_files`` 表,不依赖目录扫描;
- 同时校验 file_role / owner / conversation,返回**结构化**的
  :class:`ResolvedFileReference` 对象,而不是一个路径字符串;
- 配套 8 种细粒度失败 metadata,供 WordExportTool 在
  ``EXPORT_TEMPLATE_NOT_FOUND`` 错误码下保留真实阶段;
- 路径拼接走 ``LocalFileStorageService``,禁止工具自行拼
  ``local_storage._base / row[0]``;
- 不输出完整绝对路径、日志只输出 basename / size / 命中分支。

依赖:SQLAlchemy 异步 Session,设计上由调用方提供(WordExportTool
有 ``context.session``;TemplateParserTool 也有)。同步路径(如
无 session 的工具)通过 :func:`resolve_template_file_sync` 退化到
``sync_engine``,行为一致。
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.local_storage import local_storage

logger = logging.getLogger(__name__)


# ── 失败阶段 (内部 metadata) ─────────────────────────────────────

# 用户外部错误码保持向后兼容,内部 metadata 用于日志定位真实失败阶段
class FileRefFailure:
    REFERENCE_MISSING = "TEMPLATE_REFERENCE_MISSING"
    REFERENCE_INVALID = "TEMPLATE_REFERENCE_INVALID"
    RECORD_NOT_FOUND = "TEMPLATE_RECORD_NOT_FOUND"
    OWNER_MISMATCH = "TEMPLATE_OWNER_MISMATCH"
    ROLE_INVALID = "TEMPLATE_ROLE_INVALID"
    STORAGE_PATH_MISSING = "TEMPLATE_STORAGE_PATH_MISSING"
    PHYSICAL_FILE_NOT_FOUND = "TEMPLATE_PHYSICAL_FILE_NOT_FOUND"
    PATH_RESOLUTION_FAILED = "TEMPLATE_PATH_RESOLUTION_FAILED"


@dataclass
class ResolvedFileReference:
    """Normalized view of an uploaded_files row + storage path resolution.

    - ``internal_id`` / ``public_id`` are mutually exclusive columns.
    - ``absolute_path`` is the canonical, ``Path.exists()``-verified
      on-disk location.
    - ``owner_match`` / ``role_match`` / ``conversation_match`` are
      boolean assertions; not strict requirements (e.g. cross-user
      template share could legitimately be ``owner_match=False`` in
      future, but for Phase 2.9A.13 we treat as warning).
    """

    internal_id: int
    public_id: str
    original_name: str
    stored_name: str
    storage_path: str  # DB-stored relative path (e.g. "uploads/1/c1/file.docx")
    absolute_path: str  # On-disk absolute path (verified exists)
    mime_type: Optional[str] = None
    file_role: Optional[str] = None
    owner_internal_id: Optional[int] = None
    file_size: int = 0
    exists: bool = True
    owner_match: bool = True
    role_match: bool = True
    conversation_match: bool = True
    storage_backend: str = "local"
    failure_stage: Optional[str] = None  # None when successful
    failure_detail: Optional[str] = None
    staged_path: bool = False

    def cleanup_staged_file(self) -> None:
        """Remove a short-lived OSS materialization after a path-only tool runs."""
        if self.staged_path and self.absolute_path:
            try:
                os.unlink(self.absolute_path)
            except FileNotFoundError:
                pass

    def to_log_dict(self) -> dict:
        """Return diagnostic fields safe to log — no absolute paths."""
        return {
            "template_ref_type": self.failure_detail or (
                "public_id" if self.public_id.startswith("file_") else "internal_id"
            ),
            "internal_id": self.internal_id,
            "public_id": self.public_id,
            "basename": os.path.basename(self.absolute_path) if self.absolute_path else None,
            "path_exists": self.exists,
            "file_size": self.file_size,
            "storage_backend": self.storage_backend,
            "owner_match": self.owner_match,
            "file_role": self.file_role,
            "failure_stage": self.failure_stage,
        }


# ── 解析函数 ─────────────────────────────────────────────────────


def _is_public_id(ref: str) -> bool:
    """Heuristic: ``public_id`` always starts with ``file_``.

    Internal ID is a pure decimal string in ``uploaded_files.id``.
    Anything else (file_xxx, file_xx, etc.) is treated as public_id.
    """
    if not isinstance(ref, str) or not ref:
        return False
    return ref.startswith("file_")


def _is_internal_id_string(ref: str) -> bool:
    """Pure decimal integer string → treat as internal id.

    This intentionally does NOT consider ``file_112`` style hybrid;
    those are treated as malformed public_id (caller may have
    concatenated prefixes). The ``file_`` prefix is reserved for
    public_id by :func:`generate_public_id`.
    """
    if not isinstance(ref, str) or not ref:
        return False
    return ref.isdigit()


def _stage_oss_object(storage_path: str, original_name: str) -> str:
    """Create a temporary path for legacy Word/parser APIs that require one."""
    from app.storage.oss_storage import object_storage

    suffix = Path(original_name).suffix
    descriptor, temporary_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(object_storage.read_bytes_sync(storage_path))
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise
    return temporary_path


async def resolve_file_reference_async(
    session: AsyncSession,
    file_reference: Any,
    *,
    expected_role: Optional[str] = "test_plan_template",
    user_internal_id: Optional[int] = None,
    conversation_internal_id: Optional[int] = None,
    task_internal_id: Optional[int] = None,
) -> ResolvedFileReference:
    """Resolve a file reference (public_id or internal_id) to a normalized object.

    Args:
        session: async DB session.
        file_reference: public_id (``file_xxx``), internal_id (int
            or digit string), or ``None``/empty.
        expected_role: when set, ``UploadedFile.file_type`` must match.
        user_internal_id: when set, assert owner equals.
        conversation_internal_id: when set, warn (not fail) if mismatched.
        task_internal_id: only used for log context.

    Returns:
        :class:`ResolvedFileReference` — always returns an object, even
        on failure. The ``failure_stage`` field carries the granular
        reason; ``exists=False`` indicates the lookup chain exhausted
        without finding a usable file.
    """
    # Lazy import to avoid circular import at module load
    from app.models.uploaded_file import UploadedFile

    log_extra = {
        "task_internal_id": task_internal_id,
        "user_internal_id": user_internal_id,
    }

    # ── 1. Reference present? ──────────────────────────────────
    if file_reference is None or file_reference == "":
        logger.warning(
            "resolve_file_reference_async: 引用为空 | role=%s | %s",
            expected_role, log_extra,
        )
        return ResolvedFileReference(
            internal_id=0,
            public_id="",
            original_name="",
            stored_name="",
            storage_path="",
            absolute_path="",
            exists=False,
            failure_stage=FileRefFailure.REFERENCE_MISSING,
            failure_detail="empty_reference",
        )

    ref_str = str(file_reference).strip()
    if not ref_str:
        return ResolvedFileReference(
            internal_id=0, public_id="", original_name="", stored_name="",
            storage_path="", absolute_path="", exists=False,
            failure_stage=FileRefFailure.REFERENCE_MISSING,
            failure_detail="blank_reference",
        )

    # ── 2. Look up row by either public_id or internal_id ──────
    stmt = select(UploadedFile).where(UploadedFile.deleted_at.is_(None))
    if _is_public_id(ref_str):
        stmt = stmt.where(UploadedFile.public_id == ref_str)
        ref_kind = "public_id"
    elif _is_internal_id_string(ref_str):
        stmt = stmt.where(UploadedFile.id == int(ref_str))
        ref_kind = "internal_id"
    else:
        # Some other shape: treat as public_id and let the DB miss
        logger.warning(
            "resolve_file_reference_async: 无法识别引用形态 | ref=%r | %s",
            ref_str, log_extra,
        )
        return ResolvedFileReference(
            internal_id=0, public_id=ref_str, original_name="", stored_name="",
            storage_path="", absolute_path="", exists=False,
            failure_stage=FileRefFailure.REFERENCE_INVALID,
            failure_detail=ref_kind or "unknown_shape",
        )

    result = await session.execute(stmt)
    row = result.scalar_one_or_none()

    if row is None:
        logger.warning(
            "resolve_file_reference_async: DB 记录不存在 | ref=%r | kind=%s | %s",
            ref_str, ref_kind, log_extra,
        )
        return ResolvedFileReference(
            internal_id=int(ref_str) if ref_kind == "internal_id" else 0,
            public_id=ref_str if ref_kind == "public_id" else "",
            original_name="", stored_name="", storage_path="", absolute_path="",
            exists=False,
            failure_stage=FileRefFailure.RECORD_NOT_FOUND,
            failure_detail=ref_kind,
        )

    # ── 3. Role check ──────────────────────────────────────────
    role_match = (
        expected_role is None
        or (row.file_type or "") == expected_role
    )
    if not role_match:
        logger.warning(
            "resolve_file_reference_async: 角色不匹配 | ref=%r | file_type=%s | expected=%s",
            ref_str, row.file_type, expected_role,
        )

    # ── 4. Owner check (advisory — log only when mismatched) ────
    owner_match = (
        user_internal_id is None
        or row.user_id == user_internal_id
    )
    if not owner_match:
        logger.warning(
            "resolve_file_reference_async: owner 不匹配 | ref=%r | row_user=%s | current_user=%s",
            ref_str, row.user_id, user_internal_id,
        )

    conversation_match = (
        conversation_internal_id is None
        or row.conversation_id == conversation_internal_id
    )
    if not conversation_match:
        # 允许跨 conversation 复用模板;但记入日志
        logger.info(
            "resolve_file_reference_async: conversation 不一致 | ref=%r | row_conv=%s | current_conv=%s",
            ref_str, row.conversation_id, conversation_internal_id,
        )

    # ── 5. Storage path → absolute path ────────────────────────
    storage_path = row.storage_path or ""
    if not storage_path:
        return ResolvedFileReference(
            internal_id=row.id,
            public_id=row.public_id,
            original_name=row.original_name or "",
            stored_name=row.stored_name or "",
            storage_path="",
            absolute_path="",
            mime_type=row.mime_type,
            file_role=row.file_type,
            owner_internal_id=row.user_id,
            file_size=int(row.file_size or 0),
            exists=False,
            owner_match=owner_match,
            role_match=role_match,
            conversation_match=conversation_match,
            storage_backend=row.storage_type or "local",
            failure_stage=FileRefFailure.STORAGE_PATH_MISSING,
            failure_detail=ref_kind,
        )

    if (row.storage_type or "local") == "oss":
        try:
            staged_path = _stage_oss_object(storage_path, row.original_name or row.stored_name or "")
        except Exception as exc:
            logger.warning("resolve_file_reference_async: OSS stage failed | ref=%r | err=%s", ref_str, exc)
            return ResolvedFileReference(
                internal_id=row.id, public_id=row.public_id,
                original_name=row.original_name or "", stored_name=row.stored_name or "",
                storage_path=storage_path, absolute_path="", mime_type=row.mime_type,
                file_role=row.file_type, owner_internal_id=row.user_id,
                file_size=int(row.file_size or 0), exists=False,
                owner_match=owner_match, role_match=role_match,
                conversation_match=conversation_match, storage_backend="oss",
                failure_stage=FileRefFailure.PHYSICAL_FILE_NOT_FOUND, failure_detail=ref_kind,
            )
        return ResolvedFileReference(
            internal_id=row.id, public_id=row.public_id,
            original_name=row.original_name or "", stored_name=row.stored_name or "",
            storage_path=storage_path, absolute_path=staged_path, mime_type=row.mime_type,
            file_role=row.file_type, owner_internal_id=row.user_id,
            file_size=int(row.file_size or 0), exists=True,
            owner_match=owner_match, role_match=role_match,
            conversation_match=conversation_match, storage_backend="oss",
            failure_stage=None, failure_detail=ref_kind, staged_path=True,
        )

    try:
        # local_storage._base is already resolved.  Use forward slashes
        # for cross-platform safety: pathlib on Windows accepts both,
        # but DB rows consistently store forward slashes when written
        # via the LocalFileStorageService.  Old rows (pre-2.9A) may
        # store backslashes; Path on Windows treats both equivalently
        # for the ``/`` operator.
        normalized = storage_path.replace("\\", "/")
        absolute = (local_storage._base / normalized).resolve()
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception(
            "resolve_file_reference_async: 路径解析失败 | ref=%r | err=%s",
            ref_str, exc,
        )
        return ResolvedFileReference(
            internal_id=row.id,
            public_id=row.public_id,
            original_name=row.original_name or "",
            stored_name=row.stored_name or "",
            storage_path=storage_path,
            absolute_path="",
            mime_type=row.mime_type,
            file_role=row.file_type,
            owner_internal_id=row.user_id,
            file_size=int(row.file_size or 0),
            exists=False,
            owner_match=owner_match,
            role_match=role_match,
            conversation_match=conversation_match,
            storage_backend=row.storage_type or "local",
            failure_stage=FileRefFailure.PATH_RESOLUTION_FAILED,
            failure_detail=ref_kind,
        )

    if not absolute.exists():
        return ResolvedFileReference(
            internal_id=row.id,
            public_id=row.public_id,
            original_name=row.original_name or "",
            stored_name=row.stored_name or "",
            storage_path=storage_path,
            absolute_path=str(absolute),
            mime_type=row.mime_type,
            file_role=row.file_type,
            owner_internal_id=row.user_id,
            file_size=int(row.file_size or 0),
            exists=False,
            owner_match=owner_match,
            role_match=role_match,
            conversation_match=conversation_match,
            storage_backend=row.storage_type or "local",
            failure_stage=FileRefFailure.PHYSICAL_FILE_NOT_FOUND,
            failure_detail=ref_kind,
        )

    try:
        file_size = int(absolute.stat().st_size)
    except OSError:
        file_size = int(row.file_size or 0)

    return ResolvedFileReference(
        internal_id=row.id,
        public_id=row.public_id,
        original_name=row.original_name or "",
        stored_name=row.stored_name or "",
        storage_path=storage_path,
        absolute_path=str(absolute),
        mime_type=row.mime_type,
        file_role=row.file_type,
        owner_internal_id=row.user_id,
        file_size=file_size,
        exists=True,
        owner_match=owner_match,
        role_match=role_match,
        conversation_match=conversation_match,
        storage_backend=row.storage_type or "local",
        failure_stage=None,
        failure_detail=ref_kind,
    )


def resolve_file_reference_sync(
    file_reference: Any,
    *,
    expected_role: Optional[str] = "test_plan_template",
    user_internal_id: Optional[int] = None,
    conversation_internal_id: Optional[int] = None,
    task_internal_id: Optional[int] = None,
) -> ResolvedFileReference:
    """Synchronous variant for tools without an async session.

    Mirrors :func:`resolve_file_reference_async` but uses
    ``sync_engine`` (mysql+pymysql).  Reserved for TemplateParserTool
    and DocxFormatCheckTool which historically ran on sync_engine
    inside async tools.
    """
    from app.db.session import sync_engine
    from sqlalchemy import text

    log_extra = {
        "task_internal_id": task_internal_id,
        "user_internal_id": user_internal_id,
    }

    if file_reference is None or file_reference == "":
        return ResolvedFileReference(
            internal_id=0, public_id="", original_name="", stored_name="",
            storage_path="", absolute_path="", exists=False,
            failure_stage=FileRefFailure.REFERENCE_MISSING,
            failure_detail="empty_reference",
        )

    ref_str = str(file_reference).strip()
    if not ref_str:
        return ResolvedFileReference(
            internal_id=0, public_id="", original_name="", stored_name="",
            storage_path="", absolute_path="", exists=False,
            failure_stage=FileRefFailure.REFERENCE_MISSING,
            failure_detail="blank_reference",
        )

    if _is_public_id(ref_str):
        where_clause = "public_id = :ref"
        ref_kind = "public_id"
    elif _is_internal_id_string(ref_str):
        where_clause = "id = :ref_int"
        ref_kind = "internal_id"
    else:
        return ResolvedFileReference(
            internal_id=0, public_id=ref_str, original_name="", stored_name="",
            storage_path="", absolute_path="", exists=False,
            failure_stage=FileRefFailure.REFERENCE_INVALID,
            failure_detail="unknown_shape",
        )

    sql = (
        "SELECT id, public_id, original_name, stored_name, storage_path, mime_type, "
        "file_type, user_id, conversation_id, file_size, storage_type "
        f"FROM uploaded_files WHERE {where_clause} AND deleted_at IS NULL LIMIT 1"
    )
    params: dict[str, Any] = {"ref": ref_str}
    if ref_kind == "internal_id":
        params = {"ref_int": int(ref_str)}

    try:
        with sync_engine.connect() as conn:
            row = conn.execute(text(sql), params).fetchone()
    except Exception as exc:
        logger.warning(
            "resolve_file_reference_sync: DB 查询失败 | ref=%r | err=%s",
            ref_str, exc,
        )
        return ResolvedFileReference(
            internal_id=0, public_id=ref_str, original_name="", stored_name="",
            storage_path="", absolute_path="", exists=False,
            failure_stage=FileRefFailure.RECORD_NOT_FOUND,
            failure_detail=ref_kind,
        )

    if row is None:
        return ResolvedFileReference(
            internal_id=int(ref_str) if ref_kind == "internal_id" else 0,
            public_id=ref_str if ref_kind == "public_id" else "",
            original_name="", stored_name="", storage_path="", absolute_path="",
            exists=False,
            failure_stage=FileRefFailure.RECORD_NOT_FOUND,
            failure_detail=ref_kind,
        )

    (
        row_id, row_public_id, original_name, stored_name, storage_path,
        mime_type, file_type, row_user_id, row_conv_id, file_size, storage_type,
    ) = row

    role_match = expected_role is None or (file_type or "") == expected_role
    owner_match = user_internal_id is None or row_user_id == user_internal_id
    conversation_match = (
        conversation_internal_id is None or row_conv_id == conversation_internal_id
    )

    if not storage_path:
        return ResolvedFileReference(
            internal_id=int(row_id),
            public_id=row_public_id,
            original_name=original_name or "",
            stored_name=stored_name or "",
            storage_path="",
            absolute_path="",
            mime_type=mime_type,
            file_role=file_type,
            owner_internal_id=int(row_user_id) if row_user_id else None,
            file_size=int(file_size or 0),
            exists=False,
            owner_match=owner_match, role_match=role_match,
            conversation_match=conversation_match,
            storage_backend=storage_type or "local",
            failure_stage=FileRefFailure.STORAGE_PATH_MISSING,
            failure_detail=ref_kind,
        )

    if (storage_type or "local") == "oss":
        try:
            staged_path = _stage_oss_object(storage_path, original_name or stored_name or "")
        except Exception:
            return ResolvedFileReference(
                internal_id=int(row_id), public_id=row_public_id,
                original_name=original_name or "", stored_name=stored_name or "",
                storage_path=storage_path, absolute_path="", mime_type=mime_type,
                file_role=file_type, owner_internal_id=int(row_user_id) if row_user_id else None,
                file_size=int(file_size or 0), exists=False,
                owner_match=owner_match, role_match=role_match,
                conversation_match=conversation_match, storage_backend="oss",
                failure_stage=FileRefFailure.PHYSICAL_FILE_NOT_FOUND, failure_detail=ref_kind,
            )
        return ResolvedFileReference(
            internal_id=int(row_id), public_id=row_public_id,
            original_name=original_name or "", stored_name=stored_name or "",
            storage_path=storage_path, absolute_path=staged_path, mime_type=mime_type,
            file_role=file_type, owner_internal_id=int(row_user_id) if row_user_id else None,
            file_size=int(file_size or 0), exists=True,
            owner_match=owner_match, role_match=role_match,
            conversation_match=conversation_match, storage_backend="oss",
            failure_stage=None, failure_detail=ref_kind, staged_path=True,
        )

    try:
        normalized = storage_path.replace("\\", "/")
        absolute = (local_storage._base / normalized).resolve()
    except Exception as exc:  # pragma: no cover
        return ResolvedFileReference(
            internal_id=int(row_id),
            public_id=row_public_id,
            original_name=original_name or "",
            stored_name=stored_name or "",
            storage_path=storage_path,
            absolute_path="",
            mime_type=mime_type,
            file_role=file_type,
            owner_internal_id=int(row_user_id) if row_user_id else None,
            file_size=int(file_size or 0),
            exists=False,
            owner_match=owner_match, role_match=role_match,
            conversation_match=conversation_match,
            storage_backend=storage_type or "local",
            failure_stage=FileRefFailure.PATH_RESOLUTION_FAILED,
            failure_detail=ref_kind,
        )

    if not absolute.exists():
        return ResolvedFileReference(
            internal_id=int(row_id),
            public_id=row_public_id,
            original_name=original_name or "",
            stored_name=stored_name or "",
            storage_path=storage_path,
            absolute_path=str(absolute),
            mime_type=mime_type,
            file_role=file_type,
            owner_internal_id=int(row_user_id) if row_user_id else None,
            file_size=int(file_size or 0),
            exists=False,
            owner_match=owner_match, role_match=role_match,
            conversation_match=conversation_match,
            storage_backend=storage_type or "local",
            failure_stage=FileRefFailure.PHYSICAL_FILE_NOT_FOUND,
            failure_detail=ref_kind,
        )

    try:
        actual_size = int(absolute.stat().st_size)
    except OSError:
        actual_size = int(file_size or 0)

    return ResolvedFileReference(
        internal_id=int(row_id),
        public_id=row_public_id,
        original_name=original_name or "",
        stored_name=stored_name or "",
        storage_path=storage_path,
        absolute_path=str(absolute),
        mime_type=mime_type,
        file_role=file_type,
        owner_internal_id=int(row_user_id) if row_user_id else None,
        file_size=actual_size,
        exists=True,
        owner_match=owner_match, role_match=role_match,
        conversation_match=conversation_match,
        storage_backend=storage_type or "local",
        failure_stage=None,
        failure_detail=ref_kind,
    )


# ── 语义糖:导出场景快捷入口 ───────────────────────────────────────

async def resolve_template_for_export_async(
    session: AsyncSession,
    template_file_id: Any,
    *,
    user_internal_id: Optional[int] = None,
    conversation_internal_id: Optional[int] = None,
    task_internal_id: Optional[int] = None,
) -> ResolvedFileReference:
    """Convenience wrapper for WordExportTool — expected_role fixed."""
    return await resolve_file_reference_async(
        session,
        template_file_id,
        expected_role="test_plan_template",
        user_internal_id=user_internal_id,
        conversation_internal_id=conversation_internal_id,
        task_internal_id=task_internal_id,
    )


def resolve_template_for_export_sync(
    template_file_id: Any,
    *,
    user_internal_id: Optional[int] = None,
    conversation_internal_id: Optional[int] = None,
    task_internal_id: Optional[int] = None,
) -> ResolvedFileReference:
    """Synchronous version of :func:`resolve_template_for_export_async`."""
    return resolve_file_reference_sync(
        template_file_id,
        expected_role="test_plan_template",
        user_internal_id=user_internal_id,
        conversation_internal_id=conversation_internal_id,
        task_internal_id=task_internal_id,
    )


__all__ = [
    "FileRefFailure",
    "ResolvedFileReference",
    "resolve_file_reference_async",
    "resolve_file_reference_sync",
    "resolve_template_for_export_async",
    "resolve_template_for_export_sync",
]
