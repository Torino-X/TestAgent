"""F017 — KnowledgeAnswerService.

Decides whether an ordinary chat turn can be answered directly from
the company knowledge base (skipping the LLM entirely) or whether the
LLM must still be called.

Rules (per F017 spec):

  * Only attempt direct answer when ``route == CHAT_REPLY`` (and the
    user has opted in via ``direct_answer_enabled``).
  * Skip for ``agent_task`` / ``ask_for_files`` / ``existing_task_action``
    / ``clarify`` / ``unsupported`` — those are out of scope for the
    shortcut.
  * KB unreachable / errored → silent fallback: ``should_call_llm=True``,
    no exception bubbles into the chat flow.
  * High confidence (score >= 0.55 + non-empty top hit) → formatted
    reply with source attribution; ``should_call_llm=False``.
  * Medium / low confidence → still call the LLM, but feed the
    retrieved snippets to it via the ``context`` argument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.enums import MessageRoute
from app.repositories.knowledge_config_repository import KnowledgeConfigRepository
from app.services.knowledge_retrieval_service import (
    KnowledgeHit,
    KnowledgeRetrievalService,
)

logger = logging.getLogger(__name__)


# Routes that MUST NOT take the shortcut — they always need the LLM
# (or are out of scope for KB direct-answer).
_NO_DIRECT_ANSWER_ROUTES: frozenset[MessageRoute] = frozenset(
    {
        MessageRoute.AGENT_TASK,
        MessageRoute.ASK_FOR_FILES,
        MessageRoute.EXISTING_TASK_ACTION,
        MessageRoute.CLARIFY,
        MessageRoute.UNSUPPORTED,
    }
)


@dataclass(slots=True)
class KnowledgeAnswerDecision:
    should_call_llm: bool
    confidence: str
    direct_answer: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    source_attribution: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    error_code: str | None = None


class KnowledgeAnswerService:
    """Decide whether KB results can replace the LLM reply."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._retrieval = KnowledgeRetrievalService(session)

    async def decide(
        self,
        *,
        user_internal_id: int,
        query: str,
        route: MessageRoute,
        knowledge_ids: list[str] | None = None,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        labels: list[dict[str, Any]] | None = None,
    ) -> KnowledgeAnswerDecision:
        # 1. Only chat_reply or unsupported (KNOWLEDGE_QUESTION) routes are eligible.
        # F026: the IntentRouter routes KNOWLEDGE_QUESTION as
        # ``route=unsupported``; widen the gate here so the upstream
        # ``MessageService._kb_trigger_eligible`` check (which also looks
        # at the intent) can pass that case through.
        if route not in (MessageRoute.CHAT_REPLY, MessageRoute.UNSUPPORTED):
            return KnowledgeAnswerDecision(
                should_call_llm=True,
                confidence="low",
                reason=f"route={route.value} 不做 KB 直返",
            )

        # 2. Honour per-user toggle
        repo = KnowledgeConfigRepository(self._session)
        row = await repo.get_for_user(user_internal_id)
        if row is None or not row.direct_answer_enabled:
            return KnowledgeAnswerDecision(
                should_call_llm=True,
                confidence="low",
                reason="direct_answer_enabled=false",
            )

        # 3. Try retrieval; never raise into the chat flow
        try:
            result = await self._retrieval.retrieve(
                user_internal_id=user_internal_id,
                query=query,
                knowledge_ids=knowledge_ids,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                labels=labels,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "KnowledgeAnswerService.decide: 检索异常 fallback LLM | user=%d | err=%s",
                user_internal_id,
                exc.__class__.__name__,
            )
            return KnowledgeAnswerDecision(
                should_call_llm=True,
                confidence="low",
                reason="retrieval_exception_fallback",
                error_code="COMPANY_KB_UNKNOWN_ERROR",
            )

        if not result.success:
            # KB unreachable / misconfigured → silent fallback to LLM
            return KnowledgeAnswerDecision(
                should_call_llm=True,
                confidence="low",
                reason=f"kb_unavailable:{result.error_code}",
                error_code=result.error_code,
            )

        # 4. Compute decision based on confidence
        hits: list[KnowledgeHit] = result.hits
        attribution = self._build_attribution(hits)

        if result.direct_answer_eligible and result.confidence == "high":
            direct = self._build_direct_answer(hits)
            return KnowledgeAnswerDecision(
                should_call_llm=False,
                confidence="high",
                direct_answer=direct,
                context=self._build_context_for_llm(hits),
                source_attribution=attribution,
                reason="kb_high_confidence",
            )

        # Medium / low: still call LLM but feed the snippets in
        return KnowledgeAnswerDecision(
            should_call_llm=True,
            confidence=result.confidence,
            context=self._build_context_for_llm(hits),
            source_attribution=attribution,
            reason=f"kb_{result.confidence}_fallback_llm",
        )

    # ── Public helper: just retrieve, no decision logic ──────────

    async def retrieve_hits(
        self,
        *,
        user_internal_id: int,
        query: str,
        knowledge_ids: list[str] | None = None,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        labels: list[dict[str, Any]] | None = None,
    ):
        return await self._retrieval.retrieve(
            user_internal_id=user_internal_id,
            query=query,
            knowledge_ids=knowledge_ids,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            labels=labels,
        )

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _build_attribution(hits: list[KnowledgeHit]) -> list[dict[str, Any]]:
        return [
            {
                "knowledge_id": h.knowledge_id,
                "doc_id": h.doc_id,
                "doc_name": h.doc_name,
                "chunk_id": h.chunk_id,
                "chunk_title": h.chunk_title,
                "score": h.score,
                "source_type": h.source_type,
            }
            for h in hits
            if h.doc_id or h.chunk_id
        ]

    @staticmethod
    def _build_direct_answer(hits: list[KnowledgeHit]) -> str:
        if not hits:
            return ""
        top = hits[0]
        chunk_title = top.chunk_title or top.chunk_id or "(未命名分片)"
        doc_name = top.doc_name or top.doc_id or "(未命名文档)"
        knowledge_id = top.knowledge_id or "(未指定知识库)"
        score = (
            f"{top.score:.3f}"
            if isinstance(top.score, (int, float))
            else "未知"
        )
        content = top.content or ""
        body = [
            "根据公司知识库，命中以下内容：",
            "",
            "【知识片段】",
            content,
            "",
            "【来源】",
            f"- 文档：{doc_name}",
            f"- 分片：{chunk_title}",
            f"- 知识库：{knowledge_id}",
            f"- 置信度：{score}",
            "",
            "如果你需要，我也可以继续结合当前项目上下文解释这条规范如何应用。",
        ]
        return "\n".join(body)

    @staticmethod
    def _build_context_for_llm(hits: list[KnowledgeHit]) -> dict[str, Any]:
        snippets = [
            {
                "knowledge_id": h.knowledge_id,
                "doc_id": h.doc_id,
                "doc_name": h.doc_name,
                "chunk_id": h.chunk_id,
                "chunk_title": h.chunk_title,
                "content": h.content,
                "score": h.score,
                "source_type": h.source_type,
            }
            for h in hits
        ]
        return {
            "knowledge_snippets": snippets,
            "knowledge_source_count": len(snippets),
            "knowledge_categories": [
                "policy_snippets",
                "similar_project_snippets",
                "test_case_snippets",
                "constraints",
                "query_log",
                "error_code",
                "error_message",
            ],
        }


__all__ = ["KnowledgeAnswerService", "KnowledgeAnswerDecision"]

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (KB 直答生成,F024):
#
#   链路:
#     普通聊天问"我们公司如何做 XXX 评审":
#       → ChatLLMService 内部判断问题类型可直答
#         → KnowledgeAnswerService.answer(prompt, conversation_id, user_id)
#           → KnowledgeSearchTool 检索 chunks(限定 user 当前 KB 范围)
#           → 用 chunks 作为上下文构造 prompt
#           → LLMClient.generate 直答生成(不走 TestPlan 流水线)
#       → 落 normal assistant Message
#
# 关键约束(供开发者速查):
#   - 与 TestPlan 任务的 KB 检索不同:本服务出"答案",不出"工具调用";
#   - question_type 判定由 intent_router + ChatLLMService 协同;
#   - 没有 chunks → 回普通 chat reply(不强行编);
#   - 答案里不允许出现 file_xxx 内部 ID(用 allowed_literal_facts)。
