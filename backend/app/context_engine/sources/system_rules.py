"""System Rules / Call Contract / Current Goal Source Adapter。

WP-BE-09：chat.reply / intent.recognize 等 Profile 的 required_sections 声明了
SYSTEM_RULES / CURRENT_GOAL，但此前生产链路没有注册对应 adapter，导致 compose
在 required 收集阶段因 `context.source.adapter_not_found` fail-fast。

- SystemRulesSourceAdapter：提供全局系统规则（静态，来源 app.llm.task_profiles）。
- CurrentGoalSourceAdapter：把 request.current_user_message 作为 CURRENT_GOAL
  候选提供（Composer 最终仍单独注入三锚点 current_user_message；此处满足
  Selector 对 required section 的 item 要求）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.source import ContextWarning, SourceCollectResult
from app.context_engine.sources.registry import ContextSourceAdapterProtocol


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class SystemRulesSourceAdapter:
    """全局系统规则来源（固定必选，Trusted Instruction）。

    默认提供静态全局规则；``intent_rules`` 非空时，对 ``intent.recognize``
    call-site 注入完整意图路由指令（intent/route 枚举 + 路由规则）；
    ``title_rules`` 非空时，对 ``conversation.title`` call-site 注入
    标题生成指令（只输出简短标题）。避免 LLM 收到普通聊天 system
    而无专用指令，导致把标题任务当成普通问答。
    """

    source_kind = ContextKind.SYSTEM_RULES

    def __init__(
        self,
        *,
        system_rules: str | None = None,
        system_rules_provider=None,
        token_counter=None,
        intent_rules: str | None = None,
        title_rules: str | None = None,
    ) -> None:
        self._system_rules = system_rules
        self._system_rules_provider = system_rules_provider
        self._token_counter = token_counter
        self._intent_rules = intent_rules
        self._title_rules = title_rules

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        rules = self._system_rules
        if self._system_rules_provider is not None:
            try:
                rules = self._system_rules_provider(request.call_site or "chat.reply") or rules
            except Exception:  # noqa: BLE001 - static fallback remains available
                pass
        if not rules:
            rules = "你是 TestAgent，一个面向软件测试工作的对话式 AI 助手。"
        # intent.recognize：注入意图路由指令（若配置），确保 LLM 知道要输出意图 JSON。
        if self._intent_rules and (request.call_site or "").strip() == "intent.recognize":
            rules = self._intent_rules
        # conversation.title：注入标题生成指令（若配置），确保模型只输出简短标题。
        if self._title_rules and (request.call_site or "").strip() == "conversation.title":
            rules = self._title_rules
        items = [
            ContextItem(
                item_id="system_rules",
                kind=ContextKind.SYSTEM_RULES,
                source_type=SourceType.SYSTEM,
                source_ref="system_rules",
                content=rules,
                authority=100,
                priority=100,
                estimated_tokens=self._estimate(rules),
                trust=ContextTrust.TRUSTED_INSTRUCTION,
            )
        ]
        return SourceCollectResult(
            adapter_key="system_rules",
            kind=self.source_kind,
            items=items,
            attempted=True,
            degraded=False,
            latency_ms=_now_ms(),
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


class CallContractSourceAdapter:
    """LLM Call Contract 来源（固定必选，Trusted Instruction）。"""

    source_kind = ContextKind.CALL_CONTRACT

    def __init__(self, *, call_contract: str | None = None, token_counter=None) -> None:
        self._call_contract = call_contract
        self._token_counter = token_counter

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        contract = self._call_contract or (
            "输出必须符合输出合同：结构化、可解析、不得包含敏感信息。"
        )
        items = [
            ContextItem(
                item_id="call_contract",
                kind=ContextKind.CALL_CONTRACT,
                source_type=SourceType.SYSTEM,
                source_ref="call_contract",
                content=contract,
                authority=100,
                priority=90,
                estimated_tokens=self._estimate(contract),
                trust=ContextTrust.TRUSTED_INSTRUCTION,
            )
        ]
        return SourceCollectResult(
            adapter_key="call_contract",
            kind=self.source_kind,
            items=items,
            attempted=True,
            degraded=False,
            latency_ms=_now_ms(),
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


class CurrentGoalSourceAdapter:
    """当前目标来源：把 request.current_user_message 作为 CURRENT_GOAL 候选。"""

    source_kind = ContextKind.CURRENT_GOAL

    def __init__(self, *, token_counter=None) -> None:
        self._token_counter = token_counter

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        goal = (request.current_user_message or "").strip()
        if request.context_usage_baseline and not goal:
            # Brand-new idle conversations still need to satisfy the required
            # section contract without inventing content.
            return SourceCollectResult(
                adapter_key="current_goal",
                kind=self.source_kind,
                items=[
                    ContextItem(
                        item_id="context_usage_baseline_goal",
                        kind=ContextKind.CURRENT_GOAL,
                        source_type=SourceType.CONVERSATION,
                        source_ref="context_usage_baseline",
                        content="",
                        authority=100,
                        priority=100,
                        estimated_tokens=0,
                        trust=ContextTrust.TRUSTED_INSTRUCTION,
                        metadata={"context_usage_baseline": True},
                    )
                ],
                attempted=True,
                degraded=False,
                latency_ms=_now_ms(),
            )
        if not goal:
            return SourceCollectResult(
                adapter_key="current_goal",
                kind=self.source_kind,
                items=[],
                attempted=False,
                degraded=True,
                failure_code="context.source.no_current_goal",
                warnings=[
                    ContextWarning(
                        code="context.source.no_current_goal",
                        detail="current_user_message 缺失，跳过当前目标",
                        adapter_key="current_goal",
                    )
                ],
                latency_ms=_now_ms(),
            )
        items = [
            ContextItem(
                item_id=(
                    f"persisted_goal:{request.current_user_message_id}"
                    if request.context_usage_baseline and request.current_user_message_id
                    else "current_goal"
                ),
                kind=ContextKind.CURRENT_GOAL,
                source_type=SourceType.CONVERSATION,
                source_ref=(
                    str(request.current_user_message_id)
                    if request.context_usage_baseline and request.current_user_message_id
                    else "current_user_message"
                ),
                content=goal,
                authority=100,
                priority=100,
                estimated_tokens=self._estimate(goal),
                trust=ContextTrust.TRUSTED_INSTRUCTION,
                metadata={
                    "persisted_current_goal": bool(
                        request.context_usage_baseline and request.current_user_message_id
                    )
                },
            )
        ]
        return SourceCollectResult(
            adapter_key="current_goal",
            kind=self.source_kind,
            items=items,
            attempted=True,
            degraded=False,
            latency_ms=_now_ms(),
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


__all__ = [
    "SystemRulesSourceAdapter",
    "CallContractSourceAdapter",
    "CurrentGoalSourceAdapter",
]
# auto-appended module-level note: system rules source。
