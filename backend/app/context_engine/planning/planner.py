"""ContextPlanner — 确定性规则规划器（设计文档 §8.2）。

Planner 是"规则优先、模型可选"：v1.0 不调用 LLM 做规划，使用确定性规则：
- call_site → profile
- model → window
- node → task state section
- file type → source adapter
- intent → retrieval scope
- task status → mandatory anchors

输出 ContextPlan（含 section_plans / budget / fallback_chain）。
"""

from __future__ import annotations

from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineStage,
    raise_engine_error,
)
from app.context_engine.models.context import ContextPlan, ContextRequest, RetrievalQuery, SectionPlan
from app.context_engine.models.enums import RetrievalStrategy, RerankStrategy
from app.context_engine.models.profile import ContextBudget, ContextProfile, ProfileSectionSpec
from app.context_engine.planning.budget_calculator import ContextBudgetCalculator
from app.context_engine.planning.model_capability_resolver import ModelCapabilityResolver
from app.context_engine.profiles.registry import ContextProfileRegistry, get_default_profile_registry


class ContextPlanner:
    """确定性 Context 规划器。不调用 LLM、不做 I/O。"""

    def __init__(
        self,
        profile_registry: ContextProfileRegistry | None = None,
        budget_calculator: ContextBudgetCalculator | None = None,
        capability_resolver: ModelCapabilityResolver | None = None,
    ) -> None:
        self._registry = profile_registry or get_default_profile_registry()
        self._budget = budget_calculator or ContextBudgetCalculator()
        self._capabilities = capability_resolver or ModelCapabilityResolver()

    def plan(
        self,
        request: ContextRequest,
        *,
        model_context_window: int | None = None,
        profile_key: str | None = None,
    ) -> ContextPlan:
        """规划一次调用的 Context 合同。

        ``profile_key`` 显式指定时优先使用；否则按 ``request.call_site`` 解析。
        """
        resolved_key = profile_key or self._resolve_profile_key(request.call_site)
        profile = self._registry.get(resolved_key)
        # model → window（未知时走保守预算，不假设 200K）
        effective_window = model_context_window
        if effective_window is None and request.model_context_window is not None:
            effective_window = request.model_context_window

        budget = self._budget.calculate(
            model_context_window=effective_window,
            policy=profile.budget_policy,
        )

        section_plans = self._build_section_plans(profile, budget)
        retrieval_queries = self._build_retrieval_queries(profile, request)

        return ContextPlan(
            profile_key=profile.key,
            profile_version=profile.version,
            model_context_window=effective_window or budget.model_context_window,
            input_budget=budget.target_input,
            output_reserve=budget.output_reserve,
            runtime_reserve=budget.runtime_reserve,
            safety_margin=budget.safety_margin,
            soft_threshold=budget.soft_threshold,
            hard_compact_threshold=budget.hard_compact_threshold,
            absolute_threshold=budget.absolute_threshold,
            conversation_compact_threshold=budget.conversation_compact_threshold,
            section_plans=section_plans,
            retrieval_queries=retrieval_queries,
            compression_policy=profile.compression_policy,
            fallback_chain=list(profile.fallback_chain),
        )

    def resolve_profile_key(self, call_site: str) -> str:
        return self._resolve_profile_key(call_site)

    # ── 内部 ───────────────────────────────────────────────────────

    def _resolve_profile_key(self, call_site: str) -> str:
        """call-site → profile_key（委托 registry 权威映射，缺失 fail-fast）。"""
        call_site = (call_site or "").strip()
        if not call_site:
            raise_engine_error(
                code="context.plan.no_call_site",
                detail="ContextRequest.call_site 不能为空",
                stage=ContextEngineStage.PLANNING,
                retryable=False,
                recoverable=True,
            )
        return self._registry.get_for_call_site(call_site).key

    def _build_section_plans(
        self,
        profile: ContextProfile,
        budget: ContextBudget,
    ) -> dict[str, SectionPlan]:
        """构造 SectionPlan：尊重 spec.required 字段（不允许 list-of-required 全部强制 required=True）。

        WP-BE-09 修正：先前实现对 ``profile.required_sections`` 中所有 spec 都硬编码
        ``required=True``，忽略了 ``ProfileSectionSpec.required=False`` 的语义。
        对普通 Chat / Intent 路径，``INTENT_RECOGNIZE_PROFILE`` 把 TASK_STATE 放在
        ``required_sections`` 但 ``required=False``（意图识别时无 task 是合法状态），
        旧实现会把它当必选 → ``task_id=None`` 时 TaskStateSourceAdapter 返回
        ``no_task`` → ``required_failure`` → 整个 compose fail-fast。

        修复：以 spec.required 为准；required_sections 列表中 spec.required=False
        的项不会强制必选（profile 作者应使用 optional_sections 表达"软依赖"语义）。
        """
        plans: dict[str, SectionPlan] = {}
        for spec in profile.required_sections:
            # 尊重 spec 自己的 required 标志；required_sections 列表仅表示"参与 plan"
            plans[spec.kind.value] = self._section_plan(spec, budget, required=bool(spec.required))
        for spec in profile.optional_sections:
            plans[spec.kind.value] = self._section_plan(spec, budget, required=False)
        return plans

    @staticmethod
    def _section_plan(
        spec: ProfileSectionSpec,
        budget: ContextBudget,
        *,
        required: bool,
    ) -> SectionPlan:
        budget_tokens = spec.max_budget_tokens
        if budget_tokens is None:
            # 未声明单 Section 预算时，按 target_input 的一定比例分配（≤ target）
            budget_tokens = int(budget.target_input * 0.15)
        return SectionPlan(
            kind=spec.kind,
            required=required,
            budget_tokens=budget_tokens,
            source_types=list(spec.source_types),
            allow_retrieval=spec.allow_retrieval,
        )

    def _build_retrieval_queries(
        self,
        profile: ContextProfile,
        request: ContextRequest,
    ) -> list[RetrievalQuery]:
        """检索查询：仅当 Profile 允许 retrieval 且目标非空时生成。

        intent_result 携带检索范围时作为 query 主体；否则使用当前用户消息。
        """
        if profile.retrieval_policy in (None, "", "none"):
            return []
        query_text = ""
        if request.intent_result and request.intent_result.get("query"):
            query_text = str(request.intent_result["query"])
        elif request.retrieval_query:
            query_text = request.retrieval_query.strip()
        elif request.current_user_message:
            query_text = request.current_user_message.strip()
        if not query_text:
            return []
        return [
            RetrievalQuery(
                query_text=query_text,
                strategy=RetrievalStrategy.PLANNED,
                source_family=profile.retrieval_policy,
                top_k=5,
                rerank_strategy=RerankStrategy.WEIGHTED_RRF,
            )
        ]
# auto-appended module-level note: planner: ContextEngine 内部的 retrieve / select 阶段预算规划主入口。
