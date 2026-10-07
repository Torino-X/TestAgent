"""Process-level guards for the LangGraph-only dispatcher."""

from __future__ import annotations

from typing import Optional


class DispatchGuardError(RuntimeError):
    """Phase 2.8A 调度守护异常基类。

    与 ``feature_flags.LangGraphDisabledError``(Phase 2.1+ 守卫)同层。
    """


class ParallelDispatchGuardError(DispatchGuardError):
    """Reject concurrent execution attempts for the same task id."""

    def __init__(
        self,
        *,
        task_public_id: str,
        running_engine: str,
        requested_engine: str,
    ) -> None:
        self.task_public_id = task_public_id
        self.running_engine = running_engine
        self.requested_engine = requested_engine
        msg = (
            f"parallel dispatch blocked: task={task_public_id!r} already running "
            f"on engine={running_engine!r}; new request engine={requested_engine!r}"
        )
        super().__init__(msg)


# ── Phase 2.8C: 动态 Agent API 入口 Payload 校验异常 ────────────────────────


class IncrementalPayloadInvalidError(DispatchGuardError):
    """Phase 2.8C 守禁令 #32 — incremental dispatch payload 缺必含字段。

    触发场景:
    * ``dispatch_incremental_task`` payload 缺 ``incremental_intent`` /
      ``source_artifact_public_id`` / ``modification_idempotency_key`` 三个必含字段之一
    * ``dispatch_incremental_resume`` payload 缺 ``kind="incremental_resume"`` 或
      ``decision`` dict

    ApiDispatcher 内部 catch 后向上抛 422(由 FastAPI 错误处理器序列化)。
    """

    def __init__(
        self,
        *,
        task_public_id: str,
        missing_field: str,
        detail: Optional[str] = None,
    ) -> None:
        self.task_public_id = task_public_id
        self.missing_field = missing_field
        self.detail = detail
        msg = (
            f"incremental payload invalid: task={task_public_id!r} "
            f"missing field={missing_field!r}"
        )
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


class RepairPayloadInvalidError(DispatchGuardError):
    """Phase 2.8C 守禁令 #33 — repair dispatch payload 缺必含字段。

    触发场景:
    * ``dispatch_repair_task`` payload 缺 ``task_id`` 或 review_issues 字段

    ApiDispatcher 内部 catch 后向上抛 422(由 FastAPI 错误处理器序列化)。
    """

    def __init__(
        self,
        *,
        task_public_id: str,
        missing_field: str,
        detail: Optional[str] = None,
    ) -> None:
        self.task_public_id = task_public_id
        self.missing_field = missing_field
        self.detail = detail
        msg = (
            f"repair payload invalid: task={task_public_id!r} "
            f"missing field={missing_field!r}"
        )
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


__all__ = [
    "DispatchGuardError",
    "ParallelDispatchGuardError",
    "IncrementalPayloadInvalidError",
    "RepairPayloadInvalidError",
]
