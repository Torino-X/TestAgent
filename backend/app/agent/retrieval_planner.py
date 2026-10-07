"""Retrieval planning for chat and task turns."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.request_understanding import RequestUnderstandingResult


class KnowledgeMode(StrEnum):
    AUTO = "AUTO"
    MAAS_STRICT = "MAAS_STRICT"


class RetrievalPlan(BaseModel):
    conversation_history: str = "auto"
    user_memory: str = "auto"
    project_rules: str = "auto"
    current_attachments: Literal["off", "explicit_only"] = "off"
    internal_rag: str = "auto"
    maas: Literal["off", "auto", "required"] = "off"
    vision: bool = False
    factual_source_policy: str = "general_model_allowed"
    top_k: int = Field(default=5, ge=1)
    similarity_threshold: float = Field(default=0.35, ge=0.0, le=1.0)


class RetrievalPlanner:
    def plan(
        self,
        query: str,
        understanding: RequestUnderstandingResult,
        *,
        knowledge_mode: KnowledgeMode | str = KnowledgeMode.AUTO,
        attached_file_ids: list[str] | None = None,
    ) -> RetrievalPlan:
        mode = normalize_knowledge_mode(knowledge_mode)
        has_explicit_attachments = bool(attached_file_ids) or understanding.attachment_context

        current_attachments: Literal["off", "explicit_only"] = (
            "explicit_only" if has_explicit_attachments else "off"
        )
        if mode == KnowledgeMode.MAAS_STRICT:
            return RetrievalPlan(
                current_attachments=current_attachments,
                maas="required",
                vision=understanding.vision_required,
                factual_source_policy=(
                    "maas_and_explicit_attachment"
                    if current_attachments == "explicit_only"
                    else "maas_only"
                ),
            )

        enterprise = (
            understanding.target_capability == "enterprise_knowledge_qa"
            or understanding.knowledge_scope_hint == "enterprise"
            or understanding.enterprise_knowledge_likelihood >= 0.6
        )
        if enterprise:
            return RetrievalPlan(
                current_attachments=current_attachments,
                maas="auto",
                vision=understanding.vision_required,
                factual_source_policy="prefer_company_rag_when_relevant",
            )

        return RetrievalPlan(
            current_attachments=current_attachments,
            maas="off",
            vision=understanding.vision_required,
            factual_source_policy="general_model_allowed",
        )


def normalize_knowledge_mode(value: KnowledgeMode | str | None) -> KnowledgeMode:
    try:
        return KnowledgeMode(str(value or KnowledgeMode.AUTO.value))
    except ValueError:
        return KnowledgeMode.AUTO


# 模块定位:聊天 / 任务回合的检索规划
#
# 给定回合(query + attachments + intent),决定走哪种 retrieval 策略:
#   - vector_only / keyword_only / hybrid / no_retrieval
#   - top_k / similarity_threshold
#   - 选哪个 knowledge_id (per-user 配置或默认)
#
# 链路:
#   IntentRouter / MessageService
#     → RetrievalPlanner.plan(context)
#       → RetrievalExecutor.search(plan, ...)
#
# 关键约束:
#   - 检索必须 fail-safe:任何策略 plan 失败都自动降级为 no_retrieval;
#   - 上游 KB 不可用时,KB 段不调,避免空指针;
#   - plan 在 LLM 决策前生成,不阻塞对话。
