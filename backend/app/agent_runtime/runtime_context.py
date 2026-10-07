"""RuntimeContext - 节点访问外部资源的唯一通道。

硬约束:
* 不允许携带 ``AsyncSession`` / ``LLMClient`` 实例字段 (规则 10)
* ``settings_service`` 是引用(Phase 2.8D 起 Summary 节点实际调用 ``llm_config_provider``)
* 不允许携带文件句柄 / asyncio.Task 实例
* 必须 frozen + slots,防止节点意外 mutate

节点获取真正 IO 能力的方法:
* ``session_factory()`` 返回一个 ``AsyncContextManager[AsyncSession]``
  (callable 而不是实例 - 进入节点时再 ``async with`` 一次)
* ``settings_service.llm_config_provider()`` — Phase 2.8D Summary 节点调用
* ``event_sink`` / ``cancellation_service`` / ``clock`` 都是延迟调用

Phase 2.8D 增量(ADR-2.8D-6):
* ``_SettingsServiceRef`` Protocol 暴露 ``llm_config_provider()`` 方法
* Summary 节点(generate_completion_summary_node)用 ``ctx.settings_service.llm_config_provider()`` 取
  LLMClient,与 Legacy orchestrator._generate_completion_summary 完全一致
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncContextManager, Awaitable, Callable, Dict, Mapping, Optional, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.agent_runtime.context.protocols import ContextAwareLLMInvokerProtocol
    from app.context_engine.runtime.protocols import ContextEngineProtocol


class AgentEventSink(Protocol):
    """LangGraph 节点发出的事件接收器;Phase 2.0 仅 ``InMemoryEventSink``;

    Phase 2.6 扩展(向后兼容):新增 4 个 kwarg
      * ``event_id: str | None``        — UUID7;LiveAgentEventSink 自动分配
      * ``sequence_no: int | None``     — SequenceNumberAllocator 产物
      * ``idempotency_key: str | None`` — task|graph_run|node|type|seq
      * ``graph_run_id: str | None``    — 兼容旧 kwargs(从 state 推)
    旧 sink 实现可忽略这些参数(走 ``**kw``)。
    """

    async def emit(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        node_name: str,
        event_type: str,
        title: str,
        content: str,
        payload: dict | None = None,
        event_id: str | None = None,
        sequence_no: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict: ...


class CancellationService(Protocol):
    """Phase 2.0 stub;Phase 2.2 接入真实取消信号。"""

    def is_cancelled(self, task_id: str) -> bool: ...


class _SettingsServiceRef(Protocol):
    """最小接口(Phase 2.0 stub)— Phase 2.8D 扩展:
    Summary 节点需要 ``llm_config_provider()`` 取 LLMClient;故 Protocol 必须暴露。
    旧实现若缺该方法,Summary 节点走 fallback 模板兜底。
    """

    def llm_config_provider(self) -> Any: ...

    def __getattr__(self, name: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """节点读取的运行时上下文。

    Phase 2.9A.1 增量:``_intermediate_state`` 是 mutable dict,允许工具适配器
    在多次工具调用之间传递中间结果(如 ``template_structure``)。
    frozen+slots 不允许 setattr,所以必须显式声明一个字段。
    """

    user_internal_id: int
    task_internal_id: int
    conversation_internal_id: int
    session_factory: Callable[[], AsyncContextManager[AsyncSession]]
    settings_service: _SettingsServiceRef
    event_sink: AgentEventSink
    cancellation_service: CancellationService
    clock: Callable[[], datetime] = field(default=datetime.utcnow)
    # Phase 2.1: ToolAdapter 是 LangGraph 节点调工具的入口;若未注入,节点回退 None
    # 仅在生产(legacy) 与测试 stub 路径下为 None;v2 节点通过它走白名单 + 信封 + 速率
    tool_adapter: Any = None
    # Phase 2.9B.1: LLMClient 是 Preparation / Repair / Incremental 动态子图的
    # 决策主模型入口。显式声明为 frozen dataclass 字段而非 ``getattr``,否则
    # slots 类上 ``getattr(ctx, "llm_client", None)`` 恒为 None,导致动态 Agent
    # 永远因依赖缺失 fallback。LLMClient 不可序列化,绝不写入 Checkpoint State。
    llm_client: Any = None
    # Phase 2.9A.1: 工具间上下文传递槽(mutable dict,frozen 类仍可改 dict 内容)
    _intermediate_state: Dict[str, Any] = field(default_factory=dict)
    # CE-02 WP-8: Context Engine Facade 与 ContextAwareLLMInvoker（可选注入）。
    # 使用 Protocol 类型（TYPE_CHECKING 下导入），避免循环依赖；
    # 默认 None → CE-02 能力在 flag 关闭时不进入调用链。禁止 Any 标注。
    context_engine: Optional["ContextEngineProtocol"] = None
    context_llm_invoker: Optional["ContextAwareLLMInvokerProtocol"] = None
    # CE-05 WP-2: 任务级 Flag Resolver（TaskScopedFeatureFlagResolver）。
    # 任务路径节点经它读 Task-semantic/MIG Flag（冻结 Manifest）；None → 走进程级。
    task_flag_resolver: Any = None
    # Phase 4: immutable, JSON-safe Project context snapshot for node consumers.
    project_context: Optional[Mapping[str, Any]] = None
    # Explicit public identities for Context Engine source/scope boundaries.
    task_public_id: Optional[str] = None
    conversation_public_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class RuntimeContextFactory:
    """构造 ``RuntimeContext`` 的工厂 - 测试可注入假对象。"""

    user_internal_id: int
    task_internal_id: int
    conversation_internal_id: int
    session_factory: Callable[[], AsyncContextManager[AsyncSession]]
    settings_service: _SettingsServiceRef
    event_sink: AgentEventSink
    cancellation_service: CancellationService
    clock: Callable[[], datetime] = field(default=datetime.utcnow)
    tool_adapter: Any = None
    # Phase 2.9B.1: 动态子图决策主模型;与 RuntimeContext 一致不持久化。
    llm_client: Any = None
    # CE-02 WP-8: 可选注入（Protocol 类型），默认 None。
    context_engine: Optional["ContextEngineProtocol"] = None
    context_llm_invoker: Optional["ContextAwareLLMInvokerProtocol"] = None
    # CE-05 WP-2: 任务级 Flag Resolver（任务路径 None → fail closed）。
    task_flag_resolver: Any = None
    project_context: Optional[Mapping[str, Any]] = None
    task_public_id: Optional[str] = None
    conversation_public_id: Optional[str] = None

    def build(self) -> RuntimeContext:
        return RuntimeContext(
            user_internal_id=self.user_internal_id,
            task_internal_id=self.task_internal_id,
            conversation_internal_id=self.conversation_internal_id,
            session_factory=self.session_factory,
            settings_service=self.settings_service,
            event_sink=self.event_sink,
            cancellation_service=self.cancellation_service,
            clock=self.clock,
            tool_adapter=self.tool_adapter,
            llm_client=self.llm_client,
            context_engine=self.context_engine,
            context_llm_invoker=self.context_llm_invoker,
            task_flag_resolver=self.task_flag_resolver,
            project_context=self.project_context,
            task_public_id=self.task_public_id,
            conversation_public_id=self.conversation_public_id,
        )


__all__ = [
    "AgentEventSink",
    "CancellationService",
    "RuntimeContext",
    "RuntimeContextFactory",
]

# 模块定位:RuntimeContext = 节点访问外部资源的唯一通道(冻结 dataclass)
#
# 硬约束:
#   - 不允许携带 AsyncSession / LLMClient 实例字段(规则 10 ——
#     否则会被错误塞进 state,JSON 序列化失败);
#   - settings_service 是引用(Phase 2.8D Summary 节点实际走 llm_config_provider);
#   - 不允许携带文件句柄 / asyncio.Task 实例;
#   - 必须 frozen + slots,防止节点意外 mutate。
#
# 典型字段:
#   task_internal_id / user_internal_id / conversation_internal_id / session_factory
#   / settings_service / event_sink / cancellation_service / clock /
#   tool_adapter / context_llm_invoker / task_flag_resolver / ...
#
# 链路:
#   LangGraph 节点函数 node_xxx(state, *, ctx=RuntimeContext) →
#   ctx.event_sink.emit(...) / ctx.session_factory() → 数据库连接
#   ctx.tool_adapter.execute(...) → 调用 8 个 Tool
#
# 读源码:
#   这是新加工具/节点时**最常接触**的 dataclass;
#   先看这里字段再改节点。
