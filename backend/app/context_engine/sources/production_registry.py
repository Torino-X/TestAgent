"""生产 SourceAdapterRegistry 组装 — 注册全部 9 类 Source Adapter。

WP-BE-09：此前 `build_context_engine()` 生产链路只创建**空** SourceAdapterRegistry
（无任何 adapter 注册），compose 会因 `context.source.adapter_not_found` 失败。
本工厂注册生产默认 adapter，供 `build_production_context_components` 注入。

注册清单（source_kind → adapter）：
- SYSTEM_RULES  ：由 ContextComposer 的 system_rules 来源（无独立 adapter 时保持空）
- CURRENT_GOAL  ：current_goal 来自 request，无独立 adapter
- CONVERSATION  ：ConversationSourceAdapter + ConversationSummarySourceAdapter
- KNOWLEDGE     ：KnowledgeSourceAdapter（可注入 executor）
- MEMORY        ：MemorySourceAdapter（WP-BE-07 query-aware）
- PROJECT_INSTRUCTIONS：WorkspaceInstructionSourceAdapter
- TASK_STATE    ：TaskStateSourceAdapter
- EVIDENCE      ：FileDocumentSourceAdapter + ArtifactSourceAdapter

注：system_rules / call_contract / current_goal 在当前实现中由 Composer / Request
直接注入（不是 Source Adapter），因此此处不注册对应 adapter；相关 Profile 的
required_sections 仍会尝试收集 —— 无 adapter 时 orchestrator 返回 degraded
（required_failure 会 fail-fast）。为保证 required 不 fail，Chat 相关 Profile 的
SYSTEM_RULES/CURRENT_GOAL 需要 Composer 侧已注入系统规则。
"""

from __future__ import annotations

import logging
from typing import Any

from app.context_engine.models.enums import ContextKind
from app.context_engine.conversation_retention import NORMAL_RECENT_TURNS
from app.context_engine.sources.artifact import ArtifactSourceAdapter
from app.context_engine.sources.conversation import ConversationSourceAdapter
from app.context_engine.sources.conversation_document_evidence import (
    ConversationDocumentEvidenceSourceAdapter,
)
from app.context_engine.sources.file_document import FileDocumentSourceAdapter
from app.context_engine.sources.knowledge import KnowledgeSourceAdapter
from app.context_engine.sources.memory import MemorySourceAdapter
from app.context_engine.sources.registry import SourceAdapterRegistry
from app.context_engine.sources.summary import ConversationSummarySourceAdapter
from app.context_engine.sources.system_rules import (
    CallContractSourceAdapter,
    CurrentGoalSourceAdapter,
    SystemRulesSourceAdapter,
)
from app.context_engine.sources.task_state import TaskStateSourceAdapter
from app.context_engine.sources.task_document_evidence import TaskDocumentEvidenceSourceAdapter
from app.context_engine.sources.workspace_instruction import WorkspaceInstructionSourceAdapter

logger = logging.getLogger(__name__)

# Chat knowledge has a 2,000-token section budget.  Seven short factual
# excerpts preserve coverage for multi-part questions without approaching it.
DEFAULT_KNOWLEDGE_FINAL_LIMIT = 7


def build_default_source_registry(
    *,
    token_counter: Any = None,
    knowledge_executor: Any = None,
    retrieval_enabled: bool = False,
    memory_top_k: int = 5,
) -> SourceAdapterRegistry:
    """生产默认 SourceAdapterRegistry（注册全部 9 类）。"""
    registry = SourceAdapterRegistry()

    # SYSTEM_RULES / CALL_CONTRACT：全局系统指令（Required）
    # intent.recognize 需要完整意图路由指令（枚举 + 路由规则），否则 LLM
    # 收到无路由指令会按普通聊天回答。真源复用 IntentRouter._INTENT_SYSTEM_PROMPT
    # （bridge/compose 路径下系统提示词由此注入，避免两处维护）。
    intent_rules = None
    try:
        from app.agent.intent_router import _INTENT_SYSTEM_PROMPT

        intent_rules = _INTENT_SYSTEM_PROMPT
    except Exception:  # noqa: BLE001 — 注入失败退化为默认全局规则，不阻断启动
        intent_rules = None
    # conversation.title 需要"只输出简短标题"指令；否则 bridge/compose 路径
    # 的 system prompt 退化为普通聊天规则，模型会把标题任务当问答回答。
    # 真源复用 app.llm.task_profiles.TITLE_SYSTEM_PROMPT。
    title_rules = None
    try:
        from app.llm.task_profiles import TITLE_SYSTEM_PROMPT

        title_rules = TITLE_SYSTEM_PROMPT
    except Exception:  # noqa: BLE001 — 注入失败退化为默认全局规则，不阻断启动
        title_rules = None
    registry.register(
        SystemRulesSourceAdapter(
            token_counter=token_counter,
            system_rules_provider=_system_rules_from_markdown,
            intent_rules=intent_rules,
            title_rules=title_rules,
        )
    )
    registry.register(CallContractSourceAdapter(token_counter=token_counter))

    # CURRENT_GOAL：当前用户消息（Required）
    registry.register(CurrentGoalSourceAdapter(token_counter=token_counter))

    # CONVERSATION：最近消息 + 摘要
    registry.register(
        ConversationSourceAdapter(
            recent_turn_limit=NORMAL_RECENT_TURNS,
            exclude_current=True,
            token_counter=token_counter,
        )
    )
    registry.register(
        ConversationSummarySourceAdapter(token_counter=token_counter)
    )

    # KNOWLEDGE：CE-03 Retrieval（可注入 executor）
    registry.register(
        KnowledgeSourceAdapter(
            executor=knowledge_executor,
            retrieval_enabled=retrieval_enabled,
            enforce_runtime_flags=True,
            top_k=DEFAULT_KNOWLEDGE_FINAL_LIMIT,
        )
    )

    # MEMORY：WP-BE-07 query-aware（adapter 内部已按 query 重排）
    registry.register(
        MemorySourceAdapter(
            top_k=memory_top_k,
            candidate_limit=20,
        )
    )

    # PROJECT_INSTRUCTIONS：Conversation Project Rules（conversation scope）
    registry.register(
        WorkspaceInstructionSourceAdapter(
            token_counter=token_counter,
            max_chars=4000,
        )
    )

    # TASK_STATE
    registry.register(
        TaskStateSourceAdapter(token_counter=token_counter)
    )

    # EVIDENCE：当前任务已解析文档 + 文件元数据 + Artifact
    registry.register(
        TaskDocumentEvidenceSourceAdapter(token_counter=token_counter)
    )
    registry.register(
        ConversationDocumentEvidenceSourceAdapter(
            executor=knowledge_executor,
            retrieval_enabled=retrieval_enabled,
            token_counter=token_counter,
            top_k=5,
        )
    )
    registry.register(
        FileDocumentSourceAdapter(token_counter=token_counter)
    )
    registry.register(
        ArtifactSourceAdapter(token_counter=token_counter)
    )

    return registry


def _system_rules_from_markdown(call_site: str) -> str:
    from app.context_engine.system_instruction_catalog import render_system_instructions

    return render_system_instructions(call_site)


__all__ = ["build_default_source_registry"]
# auto-appended module-level note: 生产环境 source registry。
