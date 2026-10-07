"""Application exceptions and error codes."""

from __future__ import annotations

from typing import Any

# ── Error code ranges ─────────────────────────────────────────────────
# 40001 — 40099 : Request validation
# 40101 — 40199 : Authentication / authorization
# 40301 — 40399 : Forbidden (resource access)
# 40401 — 40499 : Not found
# 40901 — 40999 : Conflict
# 50001 — 50099 : Server / internal errors
# 50101 — 50199 : Not implemented (placeholder)


class AppError(Exception):
    """Base application error with code and message."""

    def __init__(self, code: int, message: str, detail: Any = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)


# ── Auth errors ────────────────────────────────────────────────────────

class InvalidCredentialsError(AppError):
    def __init__(self) -> None:
        super().__init__(40101, "账号或密码错误")


class TokenExpiredError(AppError):
    def __init__(self) -> None:
        super().__init__(40102, "登录已过期，请重新登录")


class UnauthorizedError(AppError):
    def __init__(self, message: str = "未登录，请先登录") -> None:
        super().__init__(40103, message)


# ── Forbidden errors ───────────────────────────────────────────────────

class ForbiddenError(AppError):
    def __init__(self) -> None:
        super().__init__(40301, "无权访问该资源")


# ── Not-found errors ───────────────────────────────────────────────────

class NotFoundError(AppError):
    def __init__(self, resource: str = "资源") -> None:
        super().__init__(40401, f"{resource}不存在")


# ── Validation errors ──────────────────────────────────────────────────

class ValidationError(AppError):
    def __init__(self, message: str = "参数错误") -> None:
        super().__init__(40001, message)


# ── Conflict errors ────────────────────────────────────────────────────

class FileExistsError_(AppError):
    def __init__(self) -> None:
        super().__init__(40901, "文件已存在")


class TemplateNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(40420, "模板不存在", {"error_code": "TEMPLATE_NOT_FOUND"})


class TemplateNotOwnedError(ForbiddenError):
    def __init__(self) -> None:
        AppError.__init__(self, 40320, "无权访问该模板", {"error_code": "TEMPLATE_NOT_OWNED"})


class TemplateNotPublicError(ForbiddenError):
    def __init__(self) -> None:
        AppError.__init__(self, 40321, "模板未公开", {"error_code": "TEMPLATE_NOT_PUBLIC"})


class TemplatePublishedDeleteConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(40920, "请先取消发布后再删除模板", {"error_code": "TEMPLATE_PUBLISHED_DELETE_CONFLICT"})


class TemplateUnsupportedFileTypeError(ValidationError):
    def __init__(self) -> None:
        AppError.__init__(self, 42220, "仅支持 DOCX 和 XLSX 模板文件", {"error_code": "TEMPLATE_UNSUPPORTED_FILE_TYPE"})


class TemplateFileTooLargeError(AppError):
    def __init__(self) -> None:
        super().__init__(41320, "模板文件超过上传大小上限", {"error_code": "TEMPLATE_FILE_TOO_LARGE"})


class TemplateVersionMissingError(AppError):
    def __init__(self) -> None:
        super().__init__(40421, "模板版本不存在", {"error_code": "TEMPLATE_VERSION_MISSING"})


class TemplateUseCopyFailedError(AppError):
    def __init__(self) -> None:
        super().__init__(50020, "模板文件复制失败", {"error_code": "TEMPLATE_USE_COPY_FAILED"})


class TemplateConversationNotOwnedError(ForbiddenError):
    def __init__(self) -> None:
        AppError.__init__(self, 40322, "无权访问目标会话", {"error_code": "TEMPLATE_CONVERSATION_NOT_OWNED"})


class TemplateInvalidCategoryError(ValidationError):
    def __init__(self) -> None:
        AppError.__init__(self, 42221, "模板分类不合法", {"error_code": "TEMPLATE_INVALID_CATEGORY"})


# ── Agent errors ───────────────────────────────────────────────────────

class AgentTaskError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(50001, message)


class MissingFilesError(AppError):
    def __init__(self, missing: list[str]) -> None:
        names = "、".join(missing)
        super().__init__(
            40002,
            f"缺少必要文件：{names}",
            detail={"missing_file_types": missing},
        )


class ToolExecutionError(AppError):
    def __init__(self, tool_name: str, reason: str, recoverable: bool = True) -> None:
        super().__init__(
            50002,
            f"工具 {tool_name} 执行失败：{reason}",
            detail={"tool_name": tool_name, "recoverable": recoverable},
        )


class NotSupportedError(AppError):
    def __init__(self, feature: str = "该功能") -> None:
        super().__init__(50101, f"{feature}尚未实现")


class DocumentPreviewEngineUnavailableError(AppError):
    """The Office renderer required for a faithful DOCX preview is absent."""

    def __init__(self) -> None:
        super().__init__(50102, "文档预览服务未配置 Office 转换引擎")


# ── Phase 2.8R: Agent Runtime 错误码 (504xx) ────────────────────────
# 50401 — 50499 : Engine dispatcher / execution
# 50501 — 50599 : LangGraph runtime / graph
# 50601 — 50699 : Checkpointer / Thread
# 50701 — 50799 : Artifact / Event idempotency
# 50801 — 50899 : Budget / recursion

class EngineDispatcherUnavailableError(AppError):
    """ApiDispatcher 未注入 / 不可用 — LangGraph 任务不得静默回退 Legacy。"""

    def __init__(self, detail: Any = None) -> None:
        super().__init__(50401, "Engine dispatcher unavailable", detail=detail)


class LangGraphRuntimeUnavailableError(AppError):
    """LangGraph 运行时不可用(Coordinator 初始化失败 / Registry 缺失)。"""

    def __init__(self, detail: Any = None) -> None:
        super().__init__(50402, "LangGraph runtime unavailable", detail=detail)


class LangGraphNotReadyError(AppError):
    """LangGraph Readiness 失败(Postgres / Redis / 必要迁移等)。"""

    def __init__(self, reason: str, detail: Any = None) -> None:
        super().__init__(50403, f"LangGraph not ready: {reason}", detail=detail)


class UnsupportedLegacyTaskError(AppError):
    """Historical Legacy tasks remain readable but can no longer execute."""

    def __init__(self, task_public_id: str | None = None) -> None:
        super().__init__(
            40910,
            "MIGRATION_REQUIRED: historical Legacy task execution is unsupported",
            detail={
                "error_code": "UNSUPPORTED_LEGACY_TASK",
                "migration_status": "migration-required",
                "task_public_id": task_public_id,
            },
        )


class CheckpointerUnavailableError(AppError):
    """Checkpointer 不可用且未允许 MemorySaver fallback。"""

    def __init__(self, reason: str, detail: Any = None) -> None:
        super().__init__(50404, f"Checkpointer unavailable: {reason}", detail=detail)


class InvalidEngineTypeError(AppError):
    """engine_type 不是 legacy / langgraph。Phase 2.8R-A:严格二元化。"""

    def __init__(self, detail: Any = None) -> None:
        super().__init__(50408, "Invalid engine type", detail=detail)


class InvalidGraphThreadIdError(AppError):
    """task_public_id 缺失或空,不得使用 stub-thread 兜底。"""

    def __init__(self, detail: Any = None) -> None:
        super().__init__(50601, "Invalid graph thread id", detail=detail)


class GraphVersionNotAvailableError(AppError):
    """请求的 graph_version 未注册,不得回退 default。"""

    def __init__(self, graph_name: str, graph_version: str, detail: Any = None) -> None:
        super().__init__(
            50501,
            f"Graph version not available: {graph_name}/{graph_version}",
            detail=detail,
        )


class EngineTypeMismatchError(AppError):
    """selected_engine_type 与 actual_engine_type 不一致。"""

    def __init__(self, selected: str, actual: str, detail: Any = None) -> None:
        super().__init__(
            50405,
            f"Engine type mismatch: selected={selected} actual={actual}",
            detail=detail,
        )


class ExecutionRequestConflictError(AppError):
    """Outbox 调度请求 idempotency 冲突。"""

    def __init__(self, detail: Any = None) -> None:
        super().__init__(50406, "Execution request conflict", detail=detail)


class ExecutionLeaseLostError(AppError):
    """Worker 持有 lease 期间失去(进程崩溃 / lease 过期)。"""

    def __init__(self, detail: Any = None) -> None:
        super().__init__(50407, "Execution lease lost", detail=detail)


class ArtifactIdempotencyConflictError(AppError):
    """Artifact idempotency_key 冲突,key 对应不同 input_hash。"""

    def __init__(self, key: str, detail: Any = None) -> None:
        super().__init__(
            50701,
            f"Artifact idempotency conflict: key={key}",
            detail=detail,
        )


class ArtifactAtomicWriteFailedError(AppError):
    """Artifact 文件原子写入失败。"""

    def __init__(self, path: str, detail: Any = None) -> None:
        super().__init__(
            50702,
            f"Artifact atomic write failed: path={path}",
            detail=detail,
        )


class ArtifactStorageError(AppError):
    """Phase 2.9A.21:Artifact 存储层错误(file_missing / storage_path_empty /
    unsafe_filename 等)。

    与 ArtifactNotFoundError 不同 — Artifact 存在,但物理文件不可读或
    文件名不合法;Service 层应该直接拒绝。
    """

    def __init__(self, detail: Any = None) -> None:
        super().__init__(
            50703,
            "Artifact storage invalid",
            detail=detail,
        )


class EventIdempotencyConflictError(AppError):
    """Event idempotency_key 冲突,逻辑事件重复。"""

    def __init__(self, key: str, detail: Any = None) -> None:
        super().__init__(
            50703,
            f"Event idempotency conflict: key={key}",
            detail=detail,
        )


class RecursionLimitConfigurationInvalidError(AppError):
    """recursion_limit 配置小于已知最小合法路径。"""

    def __init__(self, configured: int, min_required: int, detail: Any = None) -> None:
        super().__init__(
            50801,
            f"recursion_limit invalid: configured={configured} < min_required={min_required}",
            detail=detail,
        )


# ── Phase 2.9A.26: Message feedback errors (509xx) ────────────────
# 50901 — 目标消息不可反馈(角色错、不存在、Tool/System)


class MessageNotFeedbackableError(AppError):
    """目标消息不可被反馈 — 不存在 / 非 agent 角色 / 属于任务体系."""

    def __init__(self, message: str = "该消息不支持点赞或点踩") -> None:
        super().__init__(50901, message)


class MessageRegenerationNotLatestError(AppError):
    """目标消息不是当前会话最新可重新生成的 assistant 消息."""

    def __init__(self) -> None:
        super().__init__(50911, "只能重新生成当前会话的最新回复")


class MessageRegenerationAlreadyRunningError(AppError):
    """同一条消息已有 running 的重新生成."""

    def __init__(self) -> None:
        super().__init__(50912, "该消息正在重新生成中，请稍候")


class MessageRegenerationContextMissingError(AppError):
    """找不到生成该回复时的原始用户消息."""

    def __init__(self) -> None:
        super().__init__(50913, "无法找到原始对话上下文")
