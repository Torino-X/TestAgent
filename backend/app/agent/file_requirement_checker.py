"""File requirement checker — deterministic, no LLM.

Used by ``MessageService`` to override the LLM's intent-routing decision
when the LLM's classification conflicts with the actual file state in
the conversation.  Logic is pure and easy to unit-test.

Resolution priority for a single file's type:
    1. ``upload_status == "confirmed"`` AND ``file_type`` is set & != "unknown"
       → use ``file_type``.
    2. ``file_type`` is set & != "unknown" (regardless of upload_status)
       → use ``file_type`` (covers the rare "uploaded" but unconfirmed).
    3. Filename heuristic (Chinese / English keywords).
    4. ``None`` — file is not classifiable.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.models.uploaded_file import UploadedFile


class FileRequirementChecker:
    """Pure functions for file-requirement decisions."""

    REQUIRED_FOR_TEST_PLAN: tuple[str, ...] = ("requirement_doc", "test_plan_template")

    # Filename keywords → required file type.  Order matters: first match wins.
    _NAME_KEYWORDS: tuple[tuple[str, str], ...] = (
        ("requirement_doc", ("需求", "requirement", "prd", "spec")),
        ("test_plan_template", ("模板", "template", "测试方案", "test_plan")),
    )

    @staticmethod
    def resolve_confirmed_type(file: UploadedFile) -> Optional[str]:
        """Return the canonical file type for a single uploaded file.

        Returns ``None`` when the file is not classifiable.  The returned
        value is one of ``REQUIRED_FOR_TEST_PLAN`` or any other semantic
        type the caller cares about; callers should filter for the types
        they require.
        """
        ft = getattr(file, "file_type", None) or ""
        status = getattr(file, "upload_status", "") or ""
        if ft and ft != "unknown":
            if status == "confirmed" or status in ("uploaded", "confirmed"):
                return ft
            # Even if not "confirmed", trust the declared file_type if it
            # is a known semantic type (caller may still filter).
            return ft

        name = (getattr(file, "original_name", "") or "").lower()
        for canonical, keywords in FileRequirementChecker._NAME_KEYWORDS:
            for kw in keywords:
                if kw.lower() in name:
                    return canonical
        return None

    @classmethod
    def present_types(
        cls,
        files: Iterable[UploadedFile],
        attached_ids: Optional[set[str]] = None,
    ) -> set[str]:
        """Return the set of required file types present in ``files``.

        If ``attached_ids`` is provided, only files whose ``public_id`` is
        in that set are considered (mirrors the user-attached file filter
        in the original ``MessageService.send_message``).
        """
        types: set[str] = set()
        for f in files:
            if attached_ids is not None and getattr(f, "public_id", None) not in attached_ids:
                continue
            t = cls.resolve_confirmed_type(f)
            if t in cls.REQUIRED_FOR_TEST_PLAN:
                types.add(t)
        return types

    @classmethod
    def has_all_for_test_plan(
        cls,
        files: Iterable[UploadedFile],
        attached_ids: Optional[set[str]] = None,
    ) -> bool:
        """True when both ``requirement_doc`` and ``test_plan_template``
        are present in the supplied files."""
        present = cls.present_types(files, attached_ids)
        return set(cls.REQUIRED_FOR_TEST_PLAN).issubset(present)


# 模块定位:文件需求校验(确定性,无 LLM)
#
# MessageService 在做 IntentRouter 之前先跑这里,目的是覆盖 LLM 决策:
#   - 用户上传了 requirement + template → 必须走 agent_task;
#   - 用户上传了但缺关键字段 → 走 ask_for_files;
#   - 无附件且 query 命中 agent 关键词 → agent_task;否则 chat_reply
#
# 链路:
#   MessageService.send_message
#     → FileRequirementChecker.detect(uploaded_files)
#       → override(intent) 替代 LLM 判断
#     → IntentRouter (被压低权重)
#
# 关键约束:
#   - 纯函数,无 LLM,可单测;
#   - 优先级高于 IntentRouter;
#   - 不要在这里加 LLM 逻辑,违反确定性原则。
