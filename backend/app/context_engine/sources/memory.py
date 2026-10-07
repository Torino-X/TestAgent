"""Memory Source Adapter（CE-03 真实实现）。

CE-03 WP-6：MemorySourceAdapter **不调用 RetrievalExecutor**，直接走
``MemoryRepository.list_authoritative_active``（MySQL 权威检索）：
- 只返回 status='active'；deleted_at null；valid_from/expires_at 有效；
- user / workspace / agent_playbook scope 严格过滤；
- 按 scope authority → importance → quality_score → confidence →
  last_accessed_at → valid_from → public_id 排序；
- candidate_limit / top_k / token budget 限制；
- 审计经 RetrievalAuditService.record_memory_run（正文/Evidence 不进审计表）；
- Conversation Memory Mode：off 禁止读；Global Flag 永远优先。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.source import ContextWarning, SourceCollectResult
from app.context_engine.sources.registry import ContextSourceAdapterProtocol
from app.context_engine.sources._helpers import user_internal_id

logger = logging.getLogger(__name__)

MEMORY_SCOPE_USER = "user"
MEMORY_SCOPE_WORKSPACE = "workspace"
MEMORY_SCOPE_PLAYBOOK = "agent_playbook"


def _is_legacy_workspace_memory_enabled() -> bool:
    """历史 Workspace Memory 兼容门。

    3.0 正式 Conversation Project Space 模型下，Memory 正式类别只有
    `scope_type='user'`。`scope_type='workspace'` 路径保留为历史兼容代码，
    默认关闭；如需启用由环境变量 `LEGACY_WORKSPACE_MEMORY_ENABLED=true` 临时打开。
    """
    import os
    return os.environ.get("LEGACY_WORKSPACE_MEMORY_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def _is_project_workspace(workspace_key: object | None) -> bool:
    """Return whether a workspace belongs to the supported Project model.

    ProjectMemoryService persists durable project facts as workspace-scoped
    memories under ``project:<public_id>``.  Those records are not legacy
    conversation-workspace compatibility data and must be available to a
    Context Engine request that has been explicitly scoped to that project.
    """
    return str(workspace_key or "").strip().startswith("project:")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# WP-BE-07: Query-aware Memory Relevance（deterministic lexical scoring）。
# 与 Reranker 无关的确定性回退：lexical 命中 + importance + confidence。
# 分词：中文按单字窗口 + 英文按词（长度>1）。
_LEXICAL_STOPWORDS = {"", "的", "了", "是", "我", "你", "他", "她", "它", "我们",
                      "你们", "他们", "在", "有", "和", "与", "就", "都", "也",
                      "很", "会", "要", "把", "被", "让", "给", "对", "从", "去",
                      "来", "这", "那", "一个", "这个", "那个", "进行", "测试"}


def _tokenize_relevance(text: str) -> list[str]:
    """轻量分词：英文词（>1）+ 中文连续段按 2-gram 切分（确定性）。

    处理混合内容（如"接口统一使用v3"）：先按非字母数字/非中文边界切分，
    再分别处理英文词与中文段。
    """
    import re as _re

    tokens: list[str] = []
    for seg in _re.split(r"[^\w一-鿿]+", (text or "").lower()):
        if not seg:
            continue
        # 混合段（中英数字混排）：把英文/数字 run 与中文 run 分开
        for run in _re.findall(r"[a-z0-9_]+|[一-鿿]+", seg):
            if not run:
                continue
            if _re.fullmatch(r"[a-z0-9_]+", run):
                if len(run) > 1:
                    tokens.append(run)
            elif len(run) >= 2:
                for i in range(0, len(run) - 1):
                    gram = run[i : i + 2]
                    if gram not in _LEXICAL_STOPWORDS:
                        tokens.append(gram)
    return tokens


def relevance_score(query: str, memory_content: str, memory_title: str | None = None) -> float:
    """确定性 query↔memory 相关性（lexical overlap，[0,1]）。

    输入：query_tokens 与 content/title token 集合的交集计数，除以 query token 数。
    无 query → 0.0（不改变原排序）。query 为空 query_tokens 为空 → 0.0。
    """
    query_tokens = _tokenize_relevance(query)
    if not query_tokens:
        return 0.0
    content_tokens = set(_tokenize_relevance(memory_content or ""))
    if memory_title:
        content_tokens.update(_tokenize_relevance(memory_title))
    hits = sum(1 for t in query_tokens if t in content_tokens)
    if hits == 0:
        return 0.0
    return round(hits / len(query_tokens), 6)


async def select_authoritative_memory_records(
    repo,
    *,
    internal_uid: int,
    workspace_key: str | None,
    agent_type: str | None,
    query: str,
    candidate_limit: int = 20,
    top_k: int = 5,
    now: datetime | None = None,
) -> list:
    """Select the canonical working-set memories for CE and the usage ledger.

    Keeping this policy in one place prevents the conversation card from
    counting a different set of memories from the one Context Engine injects.
    """
    now = now or _utcnow()
    results: list = []
    results.extend(
        await repo.list_authoritative_active(
            internal_uid,
            scope_type=MEMORY_SCOPE_USER,
            workspace_key=None,
            now=now,
            limit=candidate_limit,
        )
    )
    if workspace_key and (
        _is_project_workspace(workspace_key)
        or _is_legacy_workspace_memory_enabled()
    ):
        results.extend(
            await repo.list_authoritative_active(
                internal_uid,
                scope_type=MEMORY_SCOPE_WORKSPACE,
                workspace_key=workspace_key,
                now=now,
                limit=candidate_limit,
            )
        )
    if agent_type:
        results.extend(
            await repo.list_authoritative_active(
                internal_uid,
                scope_type=MEMORY_SCOPE_PLAYBOOK,
                workspace_key=None,
                agent_type=agent_type,
                now=now,
                limit=candidate_limit,
            )
        )
    results.sort(
        key=lambda memory: (
            -relevance_score(query, memory.content or "", memory.title),
            -_scope_authority(memory.scope_type),
            -(memory.importance or 0),
            -(float(memory.confidence) if memory.confidence is not None else 0.0),
            -(memory.updated_at.timestamp() if memory.updated_at else 0),
            memory.public_id,
        )
    )
    return results[:top_k]


class MemorySourceAdapter:
    """长期记忆来源：MySQL 权威检索，不接 ES/Qdrant 双通道。"""

    source_kind = ContextKind.MEMORY

    def __init__(
        self,
        *,
        audit_service=None,
        candidate_limit: int = 20,
        top_k: int = 5,
        max_chars: int = 2000,
    ) -> None:
        self._audit_service = audit_service
        self._candidate_limit = candidate_limit
        self._top_k = top_k
        self._max_chars = max_chars

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        started = time.monotonic()
        warnings: list[ContextWarning] = []

        if not _memory_read_enabled(runtime_context):
            return SourceCollectResult(
                adapter_key="memory",
                kind=self.source_kind,
                items=[],
                warnings=[
                    ContextWarning(
                        code="context.memory.read_disabled",
                        detail="Memory read is disabled for this task/request",
                        adapter_key="memory",
                        source_kind=ContextKind.MEMORY.value,
                    )
                ],
                attempted=False,
                degraded=False,
                failure_code="context.memory.read_disabled",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        # Conversation Memory Mode：off → 禁止读（Global Flag 优先）
        mode = await self._resolve_memory_mode(runtime_context, request)
        if mode == "off":
            return SourceCollectResult(
                adapter_key="memory",
                kind=self.source_kind,
                items=[],
                warnings=[
                    ContextWarning(
                        code="context.memory.mode_off",
                        detail="Conversation Memory Mode=off，跳过记忆读取",
                        adapter_key="memory",
                        source_kind=ContextKind.MEMORY.value,
                    )
                ],
                attempted=True,
                degraded=True,
                failure_code="context.memory.mode_off",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        try:
            async with runtime_context.session_factory() as session:
                from app.repositories.context_engine_repositories import ContextMemoryRepository

                internal_uid = user_internal_id(runtime_context, request)
                repo = ContextMemoryRepository(session)
                now = _utcnow()

                items: list[ContextItem] = []
                candidate_count = 0
                for mem in await self._query_authoritative(repo, internal_uid, scope, request, now):
                    candidate_count += 1
                    items.append(
                        ContextItem(
                            item_id=f"memory:{mem.public_id}",
                            kind=ContextKind.MEMORY,
                            source_type=_memory_source_type(mem.scope_type),
                            source_ref=mem.public_id,
                            title=mem.title,
                            content=(mem.content or "")[: self._max_chars],
                            authority=_scope_authority(mem.scope_type),
                            priority=0,
                            estimated_tokens=max(1, len((mem.content or "")) // 3),
                            trust=ContextTrust.UNTRUSTED_REFERENCE,
                            freshness_at=mem.updated_at,
                            expires_at=mem.expires_at,
                            metadata={
                                "scope_type": mem.scope_type,
                                "memory_type": mem.memory_type,
                                "agent_type": mem.agent_type,
                                "memory_public_id": mem.public_id,
                            },
                        )
                    )

                run_id = None
                if items and self._audit_service is not None:
                    run_id = await self._audit_service.record_memory_run(
                        user_id=internal_uid,
                        workspace_key=scope.workspace_key,
                        scope_type=scope.workspace_key or "user",
                        candidate_count=candidate_count,
                        selected_count=len(items),
                        latency_ms=int((time.monotonic() - started) * 1000),
                        requested_top_k=self._top_k,
                    )

                return SourceCollectResult(
                    adapter_key="memory",
                    kind=self.source_kind,
                    items=items,
                    warnings=warnings,
                    retrieval_run_ids=[run_id] if run_id else [],
                    attempted=True,
                    degraded=False,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Memory collect failed: %s | detail=%s",
                type(exc).__name__, str(exc)[:200],
            )
            return SourceCollectResult(
                adapter_key="memory",
                kind=self.source_kind,
                items=[],
                warnings=[
                    ContextWarning(
                        code="context.memory.read_error",
                        detail="记忆读取失败，已降级",
                        adapter_key="memory",
                        source_kind=ContextKind.MEMORY.value,
                    )
                ],
                attempted=True,
                degraded=True,
                failure_code="context.memory.read_error",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

    async def _query_authoritative(self, repo, internal_uid: int, scope: ContextScope, request: ContextRequest, now):
        """按 3.0 正式 Conversation Project Space 模型查询权威 active 记忆。

        WP-BE-07：新增 query-aware relevance 排名。候选先按现有 authority /
        importance / confidence 排序取候选池，再按 query 相关性重排（relevance
        为主键，importance/confidence 为次键）。无 query → 保持原排序。

        CPS-03 收口后语义：
        - User Memory（scope_type='user', workspace_key IS NULL）**永远读取**；
          User Memory 属于 user_id，不随 Conversation 变化；
          不论当前 scope.workspace_key 是否为空（普通 Chat 或 Agent Task），
          User Memory 都进入候选。
        - Playbook Memory（agent_type 精确匹配）按需读取。
        - Project Memory（scope_type='workspace', workspace_key='project:...'）
          是 ProjectMemoryService 写入的正式项目记忆，会随显式 project
          workspace 进入 Context Assembly。
        - 非项目 Workspace Memory 仍是历史兼容路径，默认不进入 Context
          Assembly；仅可由显式兼容开关临时启用。
        """
        return await select_authoritative_memory_records(
            repo,
            internal_uid=internal_uid,
            workspace_key=scope.workspace_key,
            agent_type=request.agent_type,
            query=(request.retrieval_query or request.current_user_message or "").strip(),
            candidate_limit=self._candidate_limit,
            top_k=self._top_k,
            now=now,
        )

        from datetime import datetime as _dt

        results: list = []

        # User Memory：跨 Conversation 共享，CPS-03 永远读取
        results.extend(
            await repo.list_authoritative_active(
                internal_uid,
                scope_type=MEMORY_SCOPE_USER,
                workspace_key=None,
                now=now,
                limit=self._candidate_limit,
            )
        )

        # 正式 Project Memory：精确 project workspace 匹配。非 project 的
        # workspace 记忆仍保持默认关闭，避免把历史 conversation scope 的
        # 数据无意带回 Prompt。
        if scope.workspace_key and (
            _is_project_workspace(scope.workspace_key)
            or _is_legacy_workspace_memory_enabled()
        ):
            results.extend(
                await repo.list_authoritative_active(
                    internal_uid,
                    scope_type=MEMORY_SCOPE_WORKSPACE,
                    workspace_key=scope.workspace_key,
                    now=now,
                    limit=self._candidate_limit,
                )
            )

        # Playbook：agent_type 精确匹配
        if request.agent_type:
            results.extend(
                await repo.list_authoritative_active(
                    internal_uid,
                    scope_type=MEMORY_SCOPE_PLAYBOOK,
                    workspace_key=None,
                    agent_type=request.agent_type,
                    now=now,
                    limit=self._candidate_limit,
                )
            )

        # WP-BE-07：query-aware relevance 排序。
        #   primary  = relevance(query, content)
        #   secondary = scope authority → importance → confidence → 新→旧
        # 无 query（空）→ relevance=0，退化为原排序。
        query = (request.retrieval_query or request.current_user_message or "").strip()
        results.sort(
            key=lambda m: (
                -relevance_score(query, m.content or "", m.title),
                -_scope_authority(m.scope_type),
                -(m.importance or 0),
                -(float(m.confidence) if m.confidence is not None else 0.0),
                -(m.updated_at.timestamp() if m.updated_at else 0),
                m.public_id,
            )
        )
        return results[: self._top_k]

    async def _resolve_memory_mode(self, runtime_context, request: ContextRequest) -> str:
        """解析 Conversation Memory Mode：off/inherit/on。"""
        try:
            async with runtime_context.session_factory() as session:
                from sqlalchemy import select
                from app.models.conversation import Conversation
                from app.context_engine.sources._helpers import user_internal_id

                internal_user_id = user_internal_id(runtime_context, request)
                if request.conversation_id and str(request.conversation_id).isdigit():
                    result = await session.execute(
                        select(Conversation).where(
                            Conversation.id == int(request.conversation_id),
                            Conversation.user_id == internal_user_id,
                        )
                    )
                    conv = result.scalar_one_or_none()
                    if conv is not None:
                        return conv.context_memory_mode or "inherit"
                elif request.conversation_public_id or request.conversation_id:
                    public_id = request.conversation_public_id or request.conversation_id
                    result = await session.execute(
                        select(Conversation).where(
                            Conversation.public_id == public_id,
                            Conversation.user_id == internal_user_id,
                        )
                    )
                    conv = result.scalar_one_or_none()
                    if conv is not None:
                        return conv.context_memory_mode or "inherit"
        except Exception:  # noqa: BLE001
            pass
        return "inherit"


def _memory_read_enabled(runtime_context) -> bool:
    resolver = getattr(runtime_context, "task_flag_resolver", None)
    if resolver is not None:
        try:
            return bool(resolver.evaluate("CONTEXT_MEMORY_READ_ENABLED"))
        except Exception:  # noqa: BLE001 — privacy boundary fails closed
            return False
    from app.context_engine.feature_flags import get_context_engine_flags

    return bool(get_context_engine_flags().memory_read_implies_engine)


def _memory_source_type(scope_type: str) -> SourceType:
    if scope_type == MEMORY_SCOPE_WORKSPACE:
        return SourceType.WORKSPACE_MEMORY
    if scope_type == MEMORY_SCOPE_PLAYBOOK:
        return SourceType.AGENT_PLAYBOOK
    return SourceType.USER_MEMORY


def _scope_authority(scope_type: str) -> int:
    """scope authority 排序权重：playbook 最高 → workspace → user。"""
    if scope_type == MEMORY_SCOPE_PLAYBOOK:
        return 90
    if scope_type == MEMORY_SCOPE_WORKSPACE:
        return 80
    return 70
# auto-appended module-level note: memory source adapter。
