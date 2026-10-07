"""ContextScopeResolver — 从 ContextRequest 解析作用域。

作用域决定所有权校验与 Source Adapter 的查询边界。workspace_key 不
从文件名推断（由显式字段提供）。

workspace 解析优先级（整改 v2）：
1. Task Frozen Workspace（task 创建时冻结，最高优先）；
2. Conversation Explicit Workspace（会话显式字段）；
3. Conversation Fallback（会话未显式时用 conversation:{public_id}）。

thread_id 遵循 ``thread_id = task_public_id`` 约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.context_engine.errors import ContextEngineError, ContextEngineStage
from app.context_engine.models.context import ContextRequest, ContextScope
from app.context_engine.models.value_objects import WorkspaceKey

# 与 value_objects.PublicId 相同的 public id 格式（这里额外接受纯数字 internal id）
_PUBLIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


@dataclass(frozen=True)
class WorkspaceResolution:
    """workspace 解析结果：来源 + 值。"""

    workspace_key: str | None
    source: str  # task_frozen | conversation_explicit | conversation_derived | none
    conversation_public_id: str | None = None


class ScopeResolutionError(Exception):
    """作用域解析失败（内部异常，携带安全 detail）。"""

    def __init__(self, detail: str, code: str = "context.scope.invalid") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class ContextScopeResolver:
    """解析 ContextRequest → ContextScope（含 workspace 优先级与所有权校验）。"""

    def resolve(self, request: ContextRequest) -> ContextScope:
        # user_id 允许两种取值语义（WP-BE-09 普通 Chat 集成修复）：
        #   1. 内部数字 id（普通 Chat 路径：runtime_context 只有 user_internal_id，
        #      bridge 把 str(internal_id) 放进 request.user_id，如 '1'）；
        #   2. public id（AgentTask 路径：'user_xxx' 等 3-64 位小写字母/数字/下划线）。
        # 内部 id 不通过 PublicId 校验（PublicId 要求小写字母开头 + ≥3 位），
        # 但 adapter 的 owner 查询都经 _helpers.user_internal_id() 优先取
        # runtime_context.user_internal_id，request.user_id 仅作回退，因此两种
        # 取值都不破坏 owner-scope。这里接受 public_id 或纯数字 internal id，
        # 其余格式拒绝（防止任意字符串冒充 user id）。
        uid = request.user_id or ""
        if not uid:
            raise ScopeResolutionError(
                "user_id 不能为空", code="context.scope.invalid"
            )
        if not (uid.isdigit() or _PUBLIC_ID_RE.match(uid)):
            raise ScopeResolutionError(
                f"user_id 非法: {uid!r} (须为 public id 或内部数字 id)",
                code="context.scope.invalid",
            )

        # WP-BE-09：conversation_id 存在且未显式指定 workspace_key 时，
        # 派生 workspace_key = conversation:{public_id}（documented 的
        # "conversation_derived" 回退）。否则 Project Rule / RAG 的
        # conversation-scoped adapter 拿不到 workspace_key。
        workspace_key = None
        if getattr(request, "workspace_key", None):
            workspace_key = WorkspaceKey(request.workspace_key)
        elif getattr(request, "conversation_public_id", None):
            derived = f"conversation:{request.conversation_public_id}"
            try:
                workspace_key = WorkspaceKey(derived)
            except ValueError:
                workspace_key = None

        thread_id = request.thread_id
        if request.task_id:
            if thread_id is not None and thread_id != request.task_id:
                raise ScopeResolutionError(
                    "thread_id 必须等于 task_id（thread_id = task_public_id）",
                    code="context.scope.thread_id_mismatch",
                )
            thread_id = request.task_id

        return ContextScope(
            user_id=request.user_id,
            workspace_key=workspace_key,
            conversation_id=request.conversation_id,
            task_id=request.task_id,
            thread_id=thread_id,
            run_id=request.run_id,
        )

    # ── workspace 解析（优先级：task_frozen > conversation_explicit > conversation_derived）──

    def resolve_workspace(
        self,
        *,
        user_id: str,
        task_workspace: str | None = None,
        conversation_workspace: str | None = None,
        conversation_public_id: str | None = None,
    ) -> WorkspaceResolution:
        """按优先级解析 workspace。

        3.0 正式 Conversation Project Space 模型：
        - 优先读 Task 冻结的 conversation scope；
        - 其次读 Conversation 表的 context_workspace_key；
        - 最后由 conversation_public_id 派生 workspace_key = conversation:{public_id}。

        历史参数 fallback_workspace 已在 CPS-04 收口后删除（dead path，无 caller）。
        """
        if task_workspace:
            return WorkspaceResolution(workspace_key=task_workspace, source="task_frozen")
        if conversation_workspace:
            return WorkspaceResolution(
                workspace_key=conversation_workspace,
                source="conversation_explicit",
                conversation_public_id=conversation_public_id,
            )
        if conversation_public_id:
            derived = f"conversation:{conversation_public_id}"
            return WorkspaceResolution(
                workspace_key=derived,
                source="conversation_derived",
                conversation_public_id=conversation_public_id,
            )
        return WorkspaceResolution(workspace_key=None, source="none")

    # ── 所有权校验 ─────────────────────────────────────────────────

    def validate_ownership(
        self,
        *,
        user_id: str,
        owner_user_id: str,
        workspace_key: str | None = None,
        owner_workspace_key: str | None = None,
    ) -> None:
        """跨用户 / workspace 不匹配拒绝。

        - user_id 必须匹配 owner_user_id；
        - workspace 若声明必须匹配 owner_workspace_key。
        抛 ScopeResolutionError（不返回 bool，fail-fast）。
        """
        if user_id != owner_user_id:
            raise ScopeResolutionError(
                "跨用户作用域访问被拒绝",
                code="context.scope.cross_user_denied",
            )
        if workspace_key and owner_workspace_key and workspace_key != owner_workspace_key:
            raise ScopeResolutionError(
                "workspace 作用域不匹配",
                code="context.scope.workspace_mismatch",
            )

    @staticmethod
    def to_error(error: ScopeResolutionError) -> ContextEngineError:
        return ContextEngineError(
            code=error.code,
            detail=error.detail,
            stage=ContextEngineStage.SCOPE,
            retryable=False,
            recoverable=True,
        )
# auto-appended module-level note: scope resolver: 从 user_id / conversation_id 解析有效 scope。
