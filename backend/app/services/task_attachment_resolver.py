"""Resolve user attachments into task file bindings."""

from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.task_profiles import ATTACHMENT_BINDING_PROFILE
from app.models.attachment_understanding import FileSemanticProfile
from app.models.uploaded_file import UploadedFile
from app.services.task_attachment_schema_registry import (
    AttachmentRoleSchema,
    TaskAttachmentSchema,
    TaskAttachmentSchemaRegistry,
)


logger = logging.getLogger(__name__)


# BUG FIX 2026-08-19：3.0 文件自识别系统的等待预算。
# 用户上传完成后，FileUnderstandingService 异步跑 LLM 分类（通常 1-3s）。
# 用户"上传完立刻发起任务"时 profile 还在 processing；resolver 不等就
# 立刻以0 分跑 _score_role → 任务被拒，体验上像是"刚上传不能用"。
# 方案 A：在 binding 阶段对未 ready 且未终态的 profile 短轮询等 ready，
#        最多 5s。仍失败的保持原行为（落到3.0 设计的拒绝路径）。
PROFILE_READY_WAIT_TIMEOUT_S = 5.0
PROFILE_READY_POLL_INTERVAL_S = 0.1

# profile status 取值见 FileUnderstandingService：
#   pending / processing → 中间态，应当等待
#   ready               → 终态-成功
#   failed / unsupported → 终态-失败，不再等待
_PROFILE_TERMINAL_NON_READY_STATUSES = frozenset({"failed", "unsupported"})


@dataclass(frozen=True)
class ResolvedTaskAttachment:
    file_id: int
    binding_role: str
    position: int
    is_primary: bool
    binding_source: str
    confidence: float
    metadata_json: dict | None = None


@dataclass(frozen=True)
class TaskAttachmentResolveResult:
    status: str
    bindings: list[ResolvedTaskAttachment] = field(default_factory=list)
    reason: str = ""
    error_code: str | None = None
    ambiguous_role: str | None = None

    @property
    def legacy_requirement_file_id(self) -> int | None:
        return _primary_file_id(self.bindings, "requirement_source")

    @property
    def legacy_template_file_id(self) -> int | None:
        return _primary_file_id(self.bindings, "output_template")


class AttachmentBindingClassifier(Protocol):
    async def classify(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class TaskAttachmentResolver:
    AUTO_BIND_THRESHOLD = 0.85
    SECOND_STAGE_THRESHOLD = 0.65
    REQUIRED_MARGIN = 0.15

    def __init__(
        self,
        session: AsyncSession,
        *,
        registry: TaskAttachmentSchemaRegistry | None = None,
        binding_classifier: AttachmentBindingClassifier | Any | None = None,
    ) -> None:
        self._session = session
        self._registry = registry or TaskAttachmentSchemaRegistry()
        self._binding_classifier = binding_classifier

    async def resolve(
        self,
        *,
        task_type: str,
        user_message: str,
        ordered_files: list[UploadedFile],
        conversation_files: list[UploadedFile],
    ) -> TaskAttachmentResolveResult:
        schema = self._registry.for_task_type(task_type)
        if not schema.required_role_names:
            return TaskAttachmentResolveResult(status="RESOLVED", bindings=[])

        files = ordered_files or conversation_files
        profiles = await self._wait_for_profiles_ready(files)
        explicit = self._explicit_bindings(user_message, files)
        if explicit:
            incompatible = self._first_incompatible(explicit, files)
            if incompatible:
                return TaskAttachmentResolveResult(
                    status="UNSUPPORTED_ATTACHMENT",
                    reason="incompatible_explicit_binding",
                    error_code="attachment.incompatible_binding",
                )
            missing = self._missing_required(schema, explicit)
            if missing:
                return TaskAttachmentResolveResult(
                    status="MISSING_REQUIRED_ATTACHMENT",
                    reason=f"missing_required:{missing[0]}",
                )
            return TaskAttachmentResolveResult(status="RESOLVED", bindings=explicit)

        deterministic = self._deterministic_bindings(schema, files, profiles)
        if deterministic.status != "CLARIFICATION_REQUIRED" and deterministic.bindings:
            missing = self._missing_required(schema, deterministic.bindings)
            if not missing:
                return deterministic

        current_pair = self._current_message_pair_bindings(schema, files, profiles)
        if current_pair:
            return TaskAttachmentResolveResult(status="RESOLVED", bindings=current_pair)

        if self._binding_classifier is not None:
            llm_result = await self._second_stage(schema, user_message, files, profiles)
            if llm_result.status == "RESOLVED":
                return llm_result

        if deterministic.status == "CLARIFICATION_REQUIRED":
            return deterministic
        missing = self._missing_required(schema, deterministic.bindings)
        if missing:
            return TaskAttachmentResolveResult(
                status="MISSING_REQUIRED_ATTACHMENT",
                bindings=deterministic.bindings,
                reason=f"missing_required:{missing[0]}",
            )
        return deterministic

    async def _profiles_by_file_id(self, files: list[UploadedFile]) -> dict[int, FileSemanticProfile]:
        ids = [int(file.id) for file in files if getattr(file, "id", None) is not None]
        if not ids:
            return {}
        result = await self._session.execute(
            select(FileSemanticProfile).where(FileSemanticProfile.file_id.in_(ids))
        )
        return {int(profile.file_id): profile for profile in result.scalars().all()}

    async def _wait_for_profiles_ready(
        self, files: list[UploadedFile]
    ) -> dict[int, FileSemanticProfile]:
        """拉 profiles 后，对未 ready 且非终态的 profile 短轮询等到 ready。

        BUG FIX 2026-08-19（方案 A）：用户上传后立刻发任务，profile 通常
        还在 processing；不等到 ready 会让 _score_role 一律0 分，resolver
        误判为 MISSING_REQUIRED_ATTACHMENT，体验上像是"刚上传不能用"。

        等待策略：
          - 每 100ms 轮询一次，最多 5s；
          - profile.status == "ready"               → 视为可用，纳入评分；
          - profile.status ∈ {"failed","unsupported"} → 视为终态失败，不等；
          - profile 缺失或 status ∈ {"pending","processing"} → 等下一轮；
          - 超时：返回当前快照（与历史行为一致，落入3.0 的拒绝路径）。
        """
        import asyncio
        import time

        profiles = await self._profiles_by_file_id(files)
        if not profiles:
            return profiles

        deadline = time.monotonic() + PROFILE_READY_WAIT_TIMEOUT_S
        while time.monotonic() < deadline:
            pending_ids: list[int] = []
            for fid, profile in profiles.items():
                status = getattr(profile, "status", None)
                if status == "ready":
                    continue
                if status in _PROFILE_TERMINAL_NON_READY_STATUSES:
                    continue
                # pending / processing / None → 等下一轮
                pending_ids.append(fid)
            if not pending_ids:
                break
            await asyncio.sleep(PROFILE_READY_POLL_INTERVAL_S)
            # 仅重拉尚未就绪的 file_id，避免无谓 IO
            pending_files = [
                f for f in files
                if getattr(f, "id", None) is not None
                and int(getattr(f, "id")) in pending_ids
            ]
            refreshed = await self._profiles_by_file_id(pending_files)
            profiles.update(refreshed)

        still_pending = [
            int(getattr(f, "id"))
            for f in files
            if getattr(f, "id", None) is not None
            and int(getattr(f, "id")) in profiles
            and getattr(profiles[int(getattr(f, "id"))], "status", None)
            not in ("ready", *_PROFILE_TERMINAL_NON_READY_STATUSES)
        ]
        if still_pending:
            logger.info(
                "TaskAttachmentResolver: profile not ready within %.1fs | file_ids=%s",
                PROFILE_READY_WAIT_TIMEOUT_S,
                still_pending,
            )
        return profiles

    def _explicit_bindings(self, message: str, files: list[UploadedFile]) -> list[ResolvedTaskAttachment]:
        by_name = {_normalize_filename(file.original_name): file for file in files}
        bindings: list[ResolvedTaskAttachment] = []

        if re.search(r"第\s*一\s*个.*需求", message) and len(files) >= 1:
            bindings.append(_binding(files[0], "requirement_source", 0, "user_explicit", 1.0))
        if re.search(r"第\s*二\s*个.*模板", message) and len(files) >= 2:
            bindings.append(_binding(files[1], "output_template", 0, "user_explicit", 1.0))

        patterns = (
            (r"用《([^》]+)》作为需求", "requirement_source"),
            (r"用《([^》]+)》作为模板", "output_template"),
        )
        seen = {(item.file_id, item.binding_role) for item in bindings}
        for pattern, role in patterns:
            for match in re.finditer(pattern, message):
                file = by_name.get(_normalize_filename(match.group(1)))
                if file is None:
                    continue
                key = (int(file.id), role)
                if key not in seen:
                    bindings.append(_binding(file, role, 0, "user_explicit", 1.0))
                    seen.add(key)
        return _sort_bindings(bindings)

    def _deterministic_bindings(
        self,
        schema: TaskAttachmentSchema,
        files: list[UploadedFile],
        profiles: dict[int, FileSemanticProfile],
    ) -> TaskAttachmentResolveResult:
        bindings: list[ResolvedTaskAttachment] = []
        bound_file_ids: set[int] = set()
        for role in schema.roles:
            candidates = self._score_candidates(role, files, profiles, bound_file_ids)
            if role.required and role.max_count == 1:
                if not candidates or candidates[0][0] < self.AUTO_BIND_THRESHOLD:
                    return TaskAttachmentResolveResult(
                        status="MISSING_REQUIRED_ATTACHMENT",
                        bindings=bindings,
                        reason=f"missing_required:{role.name}",
                    )
                if len(candidates) > 1 and candidates[1][0] >= self.AUTO_BIND_THRESHOLD:
                    margin = candidates[0][0] - candidates[1][0]
                    if margin < self.REQUIRED_MARGIN:
                        return TaskAttachmentResolveResult(
                            status="CLARIFICATION_REQUIRED",
                            bindings=bindings,
                            reason="ambiguous_required_role",
                            ambiguous_role=role.name,
                        )
                file = candidates[0][1]
                bindings.append(_binding(file, role.name, 0, "automatic", candidates[0][0]))
                bound_file_ids.add(int(file.id))
                continue

            selected = [
                (score, file)
                for score, file in candidates
                if score >= self.AUTO_BIND_THRESHOLD
                and (role.max_count is None or len(bindings) < role.max_count)
            ]
            for position, (score, file) in enumerate(selected):
                bindings.append(_binding(file, role.name, position, "automatic", score, is_primary=position == 0))
                bound_file_ids.add(int(file.id))

        return TaskAttachmentResolveResult(status="RESOLVED", bindings=_sort_bindings(bindings))

    def _current_message_pair_bindings(
        self,
        schema: TaskAttachmentSchema,
        files: list[UploadedFile],
        profiles: dict[int, FileSemanticProfile],
    ) -> list[ResolvedTaskAttachment]:
        required_roles = [
            role
            for role in schema.roles
            if role.required and role.min_count == 1
        ]
        if len(files) != len(required_roles) or len(files) < 2:
            return []

        assignments: list[tuple[AttachmentRoleSchema, UploadedFile, float]] = []
        for role in required_roles:
            candidates = self._score_candidates(role, files, profiles, set())
            candidates = [
                (score, file)
                for score, file in candidates
                if score >= self.SECOND_STAGE_THRESHOLD
            ]
            if len(candidates) != 1:
                return []
            score, file = candidates[0]
            assignments.append((role, file, score))

        assigned_file_ids = [int(file.id) for _, file, _ in assignments]
        if len(set(assigned_file_ids)) != len(assigned_file_ids):
            return []

        return _sort_bindings(
            [
                _binding(file, role.name, 0, "current_message_pair", score)
                for role, file, score in assignments
            ]
        )

    def _score_candidates(
        self,
        role: AttachmentRoleSchema,
        files: list[UploadedFile],
        profiles: dict[int, FileSemanticProfile],
        bound_file_ids: set[int],
    ) -> list[tuple[float, UploadedFile]]:
        scored: list[tuple[float, UploadedFile]] = []
        for file in files:
            if int(file.id) in bound_file_ids:
                continue
            if not self._registry.is_compatible(role.name, file.file_ext):
                continue
            score = self._score_role(file, profiles.get(int(file.id)), role.name)
            if score > 0:
                scored.append((score, file))
        scored.sort(key=lambda item: (-item[0], _file_position(files, item[1]), int(item[1].id)))
        return scored

    @staticmethod
    def _score_role(
        file: UploadedFile,
        profile: FileSemanticProfile | None,
        role: str,
    ) -> float:
        score = 0.0
        if profile is not None and profile.status == "ready":
            usages = set(profile.possible_usages_json or [])
            if role in usages:
                score = max(score, float(profile.confidence or Decimal("0.90")))
            kind = profile.document_kind
            if role == "requirement_source" and kind == "requirements_specification":
                score = max(score, float(profile.confidence or Decimal("0.90")))
            if role == "output_template" and kind == "test_plan_template":
                score = max(score, float(profile.confidence or Decimal("0.90")))
            if role == "reference_material" and kind == "supplemental_reference":
                score = max(score, float(profile.confidence or Decimal("0.90")))

        file_type = getattr(file, "file_type", "") or ""
        if role == "requirement_source" and file_type == "requirement_doc":
            score = max(score, 0.86)
        if role == "output_template" and file_type == "test_plan_template":
            score = max(score, 0.86)
        if role == "reference_material" and file_type == "supplemental_doc":
            score = max(score, 0.86)

        name = (file.original_name or "").lower()
        if role == "requirement_source" and any(k in name for k in ("requirement", "prd", "spec", "需求")):
            score = max(score, 0.70)
        if role == "output_template" and any(k in name for k in ("template", "test_plan", "模板")):
            score = max(score, 0.70)
        return score

    async def _second_stage(
        self,
        schema: TaskAttachmentSchema,
        user_message: str,
        files: list[UploadedFile],
        profiles: dict[int, FileSemanticProfile],
    ) -> TaskAttachmentResolveResult:
        payload = {
            "profile": ATTACHMENT_BINDING_PROFILE.name,
            "user_message": user_message,
            "attachments": [
                _attachment_metadata(index, file, profiles.get(int(file.id)))
                for index, file in enumerate(files)
            ],
            "task_schema": {
                "task_type": schema.task_type,
                "required_roles": schema.required_role_names,
                "roles": [role.name for role in schema.roles],
            },
        }
        classifier = self._binding_classifier
        if hasattr(classifier, "classify"):
            result = classifier.classify(payload)
        else:
            result = classifier(payload)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            return TaskAttachmentResolveResult(status="CLARIFICATION_REQUIRED", reason="binding_classifier_failed")

        bindings: list[ResolvedTaskAttachment] = []
        by_public_id = {file.public_id: file for file in files}
        for item in result.get("bindings", []):
            if not isinstance(item, dict):
                continue
            file = by_public_id.get(str(item.get("file_public_id") or ""))
            role = str(item.get("binding_role") or "")
            if file is None or role not in {r.name for r in schema.roles}:
                continue
            if not self._registry.is_compatible(role, file.file_ext):
                return TaskAttachmentResolveResult(
                    status="UNSUPPORTED_ATTACHMENT",
                    reason="incompatible_llm_binding",
                    error_code="attachment.incompatible_binding",
                )
            confidence = _float(item.get("confidence"), 0.0)
            bindings.append(_binding(file, role, len(bindings), "llm", confidence))
        if self._missing_required(schema, bindings):
            return TaskAttachmentResolveResult(status="CLARIFICATION_REQUIRED", reason="binding_classifier_incomplete")
        return TaskAttachmentResolveResult(status="RESOLVED", bindings=_sort_bindings(bindings))

    def _first_incompatible(
        self,
        bindings: list[ResolvedTaskAttachment],
        files: list[UploadedFile],
    ) -> ResolvedTaskAttachment | None:
        by_id = {int(file.id): file for file in files}
        for binding in bindings:
            file = by_id.get(binding.file_id)
            if file is not None and not self._registry.is_compatible(binding.binding_role, file.file_ext):
                return binding
        return None

    @staticmethod
    def _missing_required(
        schema: TaskAttachmentSchema,
        bindings: list[ResolvedTaskAttachment],
    ) -> list[str]:
        role_counts: dict[str, int] = {}
        for binding in bindings:
            role_counts[binding.binding_role] = role_counts.get(binding.binding_role, 0) + 1
        return [
            role.name
            for role in schema.roles
            if role.required and role_counts.get(role.name, 0) < role.min_count
        ]


def _binding(
    file: UploadedFile,
    role: str,
    position: int,
    source: str,
    confidence: float,
    *,
    is_primary: bool = True,
) -> ResolvedTaskAttachment:
    return ResolvedTaskAttachment(
        file_id=int(file.id),
        binding_role=role,
        position=position,
        is_primary=is_primary,
        binding_source=source,
        confidence=confidence,
        metadata_json={"file_public_id": file.public_id, "filename": file.original_name},
    )


def _sort_bindings(bindings: list[ResolvedTaskAttachment]) -> list[ResolvedTaskAttachment]:
    order = {"requirement_source": 0, "output_template": 1, "reference_material": 2}
    return sorted(bindings, key=lambda item: (order.get(item.binding_role, 99), item.position, item.file_id))


def _primary_file_id(bindings: list[ResolvedTaskAttachment], role: str) -> int | None:
    candidates = [binding for binding in bindings if binding.binding_role == role and binding.is_primary]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.position, item.file_id))[0].file_id


def _file_position(files: list[UploadedFile], target: UploadedFile) -> int:
    for index, file in enumerate(files):
        if file.id == target.id:
            return index
    return len(files)


def _normalize_filename(value: str) -> str:
    return Path(value or "").name.strip().lower()


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _attachment_metadata(index: int, file: UploadedFile, profile: FileSemanticProfile | None) -> dict[str, Any]:
    return {
        "public_id": file.public_id,
        "filename": file.original_name,
        "position": index,
        "extension": file.file_ext,
        "understanding_status": getattr(profile, "status", None),
        "document_kind": getattr(profile, "document_kind", None),
        "safe_summary": getattr(profile, "summary", None),
        "possible_usages": getattr(profile, "possible_usages_json", None) or [],
    }


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (用户上传附件 → 任务文件绑定解析):
#
#   链路:
#     send_message(包含 attachment.public_ids) →
#       → TaskAttachmentResolver.resolve(conversation_id, user_id, attached_public_ids)
#         → 校验所有权(ownership=conversation_id AND user_id=owner);
#         → 校验文件状态(file_type=已知需求类型, capability_identified=True);
#         → 返回:[(internal_id, public_id, file_name, capability_hint), ...]
#       → 写入 AgentTask.requirement_file_id / template_file_id
#
#   与文件上传服务的区别:
#     FileService 只管 upload + storage;本服务管"在测试任务中能用哪个文件"
#
# 关键约束(供开发者速查):
#   - 仅返回合法 capability 的文件(未识别 cap 的不可用作 task input);
#   - 解析结果以 list[tuple] 形式返回,内部 id 不暴露给前端;
#   - 失败的 attachment 直接被丢弃(不报错),让 LLM 处理无附件情况;
#   - 用户在多 session 同时上传同名文件 → 各自按 public_id 隔离,不影响解析。
