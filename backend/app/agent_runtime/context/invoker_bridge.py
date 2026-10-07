"""ContextInvokerBridge — 把 context_llm_invoker 包装为 generate_with_profile 兼容。

CE-04 WP-9：
- **不修改业务 State**。返回 ``InvokerBridgeResult``：value + context_state_patch +
  snapshot_public_id + stats。
- **State Patch 由 Node 显式 return**：Node 收到 result 后自行把
  context_state_patch 合入自己返回的 state dict。
- 所有调用显式传入：call_site / llm_task_profile / current_node / current_goal /
  TaskStateRef / output contract。不隐式猜测。
- Feature Flag 门控：context_engine_enabled 关时返回 None（节点走既有路径）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.context_engine.models.context import ContextRequest


@dataclass(frozen=True)
class InvokerBridgeResult:
    """Bridge 返回（State-safe，不含 prompt 全文）。"""

    value: Any
    context_state_patch: dict[str, Any] = field(default_factory=dict)
    snapshot_public_id: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    def as_profile_result(self):
        """转成 LLMProfileResult 兼容形状（parsed/success/error_message）。

        供 repair/agent_loop 等以 ``raw.parsed`` / ``raw.success`` 读取的调用点使用。
        """
        class _Compat:
            parsed = self.value
            success = self.value is not None
            error_message = None if self.value is not None else "bridge_value_none"

        return _Compat()


@dataclass(frozen=True, slots=True)
class _BoundContextInvokerBridge:
    """Request-local ``generate_with_profile`` compatibility view of a shared bridge."""

    bridge: "ContextInvokerBridge"
    user_id: int
    call_site: str
    task_id: int | str | None
    runtime_context: Any

    @property
    def available(self) -> bool:
        return self.bridge.available

    @property
    def is_context_engine_bridge(self) -> bool:
        """Marker used by CE-only consumers to reject raw provider clients."""
        return True

    async def generate_with_profile(
        self,
        llm_task_profile: Any,
        user_content: str,
        *,
        parser: Any = None,
        system_prompt_override: str | None = None,
        **kwargs,
    ) -> "_CompatResult":
        del parser
        output_contract = kwargs.pop("output_contract", None)
        image_paths = kwargs.pop("images", None)
        result = await self.bridge.generate(
            user_id=self.user_id,
            call_site=self.call_site,
            llm_task_profile=llm_task_profile,
            current_goal=user_content,
            user_content=user_content,
            conversation_id=getattr(self.runtime_context, "conversation_internal_id", None),
            task_id=self.task_id,
            runtime_context=self.runtime_context,
            system_prompt=system_prompt_override,
            output_contract=output_contract,
            image_paths=image_paths,
        )
        if result is None:
            return _CompatResult(None)
        return _CompatResult(result.value)

    async def generate_with_system(
        self,
        system_prompt: str,
        user_content: str,
        *,
        timeout_override: int | None = None,
        **kwargs,
    ) -> str:
        """Compatibility API used by ``NarrativeComposer``.

        Bound narrative clients previously implemented only
        ``generate_with_profile``.  The composer therefore raised
        ``AttributeError`` as soon as MIG_NARRATIVE was enabled.  Build a
        bounded plain-text task profile and preserve the supplied system
        prompt through the Context Engine request rather than falling back to
        a raw legacy client.
        """
        from app.llm.task_profiles import LLMParserType, LLMTaskProfile

        supplied_profile = kwargs.pop("llm_task_profile", None)
        if supplied_profile is None:
            profile = LLMTaskProfile(
                name=f"{self.call_site or 'context'}.bound_system.v1",
                system_prompt=system_prompt,
                parser=LLMParserType.PLAIN_TEXT,
                allow_markdown=True,
                require_json=False,
                timeout_override=timeout_override,
            )
        elif isinstance(supplied_profile, LLMTaskProfile):
            # Keep the named call-site contract (tokens, temperature and parser)
            # while the composer supplies the request-specific tagged prompt.
            profile = supplied_profile.model_copy(
                update={
                    "system_prompt": system_prompt,
                    "timeout_override": timeout_override
                    if timeout_override is not None
                    else supplied_profile.timeout_override,
                }
            )
        else:
            raise TypeError("llm_task_profile must be an LLMTaskProfile")
        image_paths = kwargs.pop("images", None)
        result = await self.bridge.generate(
            user_id=self.user_id,
            call_site=self.call_site,
            llm_task_profile=profile,
            current_goal=user_content,
            user_content=user_content,
            conversation_id=getattr(self.runtime_context, "conversation_internal_id", None),
            task_id=self.task_id,
            runtime_context=self.runtime_context,
            system_prompt=system_prompt,
            output_contract=kwargs.pop("output_contract", "text"),
            image_paths=image_paths,
        )
        if result is None or result.value is None:
            raise RuntimeError("context_bridge_unavailable")
        return str(result.value)


class ContextInvokerBridge:
    """把 context_llm_invoker.invoke() 包装为生成调用桥。"""

    def __init__(
        self,
        *,
        invoker=None,
        state_ref_builder=None,
        enabled: bool = False,
    ) -> None:
        self._invoker = invoker
        self._state_ref_builder = state_ref_builder
        self._enabled = enabled
        # The shared bridge keeps only shared dependencies; request identity lives in bind() results.

    def bind(
        self,
        *,
        user_id: int,
        call_site: str,
        task_id: int | str | None = None,
        runtime_context=None,
    ) -> _BoundContextInvokerBridge:
        """Return a request-local compatibility view for ``generate_with_profile``."""
        return _BoundContextInvokerBridge(
            bridge=self,
            user_id=user_id,
            call_site=call_site,
            task_id=task_id,
            runtime_context=runtime_context,
        )

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    @property
    def available(self) -> bool:
        return self._enabled and self._invoker is not None

    def _build_request(
        self,
        *,
        user_id: int,
        call_site: str,
        current_node: str | None = None,
        current_goal: str | None = None,
        current_user_message_id: int | None = None,
        task_state_ref: dict[str, Any] | None = None,
        user_content: str | None = None,
        conversation_id: int | None = None,
        task_id: int | str | None = None,
        runtime_context=None,
        system_prompt: str | None = None,
        output_contract: str | None = None,
        image_paths: list[str] | None = None,
    ) -> ContextRequest:
        runtime_conversation_id = getattr(runtime_context, "conversation_internal_id", None)
        runtime_task_id = getattr(runtime_context, "task_internal_id", None)
        runtime_task_public_id = getattr(runtime_context, "task_public_id", None)
        effective_conversation_id = conversation_id or runtime_conversation_id
        effective_task_id = task_id or runtime_task_public_id or runtime_task_id
        if (
            runtime_task_public_id
            and runtime_task_id is not None
            and str(effective_task_id) == str(runtime_task_id)
        ):
            effective_task_id = runtime_task_public_id
        runtime_public_id = getattr(runtime_context, "conversation_public_id", None)
        runtime_workspace_key = getattr(runtime_context, "context_workspace_key", None)
        if not runtime_workspace_key:
            project_context = getattr(runtime_context, "project_context", None)
            if isinstance(project_context, dict):
                runtime_workspace_key = project_context.get("workspace_key")
        workspace_key = str(runtime_workspace_key).strip() if runtime_workspace_key else None

        return ContextRequest(
            user_id=str(user_id),
            conversation_id=str(effective_conversation_id) if effective_conversation_id else None,
            conversation_public_id=str(runtime_public_id) if runtime_public_id else None,
            task_id=str(effective_task_id) if effective_task_id else None,
            call_site=call_site,
            agent_type="test_plan" if call_site.startswith("test_plan.") else None,
            current_node=current_node,
            current_user_message=current_goal or user_content,
            current_user_message_id=current_user_message_id,
            system_prompt=system_prompt,
            output_contract=output_contract,
            state_ref=task_state_ref,
            workspace_key=workspace_key or None,
            image_paths=list(image_paths or []),
        )

    async def generate(
        self,
        *,
        user_id: int,
        call_site: str,
        llm_task_profile: Any,
        current_node: str | None = None,
        current_goal: str | None = None,
        current_user_message_id: int | None = None,
        task_state_ref: dict[str, Any] | None = None,
        output_contract: str | None = None,
        user_content: str | None = None,
        conversation_id: int | None = None,
        task_id: int | str | None = None,
        runtime_context=None,
        system_prompt: str | None = None,
        image_paths: list[str] | None = None,
    ) -> InvokerBridgeResult | None:
        """执行一次桥接生成。flag 关或 invoker 缺失 → None。"""
        if not self.available:
            return None

        request = self._build_request(
            user_id=user_id,
            call_site=call_site,
            current_node=current_node,
            current_goal=current_goal,
            current_user_message_id=current_user_message_id,
            task_state_ref=task_state_ref,
            user_content=user_content,
            conversation_id=conversation_id,
            task_id=task_id,
            runtime_context=runtime_context,
            system_prompt=system_prompt,
            output_contract=output_contract,
            image_paths=image_paths,
        )

        result = await self._invoker.invoke(
            request=request,
            llm_task_profile=llm_task_profile,
            runtime_context=runtime_context,
        )
        if result is None:
            return None

        value = getattr(result, "value", None)
        snapshot_public_id = getattr(result, "snapshot_public_id", None)

        # State Patch：由 StateRefBuilder 构建（不修改业务 State，返回给 Node 显式应用）
        patch: dict[str, Any] = {}
        state_ref = getattr(result, "context_state_ref", None)
        if state_ref is None and snapshot_public_id and self._state_ref_builder is not None:
            try:
                state_ref = self._state_ref_builder(
                    snapshot_public_id=snapshot_public_id,
                    profile_key=call_site,
                    token_usage=getattr(result, "token_usage", None),
                )
                patch = {"context_state": state_ref.to_state_dict()}
            except Exception:  # noqa: BLE001 — 不因 patch 失败影响 value
                patch = {}

        if state_ref is not None:
            try:
                patch = {"context_state": state_ref.to_state_dict()}
            except Exception:  # noqa: BLE001
                patch = {}

        state_stats = getattr(state_ref, "stats", None)
        stats = (
            state_stats.model_dump(mode="json")
            if state_stats is not None and hasattr(state_stats, "model_dump")
            else {}
        )
        stats.update(
            {
                "snapshot_public_id": snapshot_public_id,
                "attempts": len(getattr(result, "attempts", ())),
            }
        )

        return InvokerBridgeResult(
            value=value,
            context_state_patch=patch,
            snapshot_public_id=snapshot_public_id,
            stats=stats,
        )

    async def stream(
        self,
        *,
        user_id: int,
        call_site: str,
        llm_task_profile: Any,
        current_node: str | None = None,
        current_goal: str | None = None,
        current_user_message_id: int | None = None,
        task_state_ref: dict[str, Any] | None = None,
        output_contract: str | None = None,
        user_content: str | None = None,
        conversation_id: int | None = None,
        task_id: int | str | None = None,
        runtime_context=None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        """执行一次桥接流式生成。flag 关或 invoker 缺失 → 不产生 chunk。"""
        if not self.available:
            return

        invoker_stream = getattr(self._invoker, "stream", None)
        if invoker_stream is None:
            result = await self.generate(
                user_id=user_id,
                call_site=call_site,
                llm_task_profile=llm_task_profile,
                current_node=current_node,
                current_goal=current_goal,
                current_user_message_id=current_user_message_id,
                task_state_ref=task_state_ref,
                user_content=user_content,
                conversation_id=conversation_id,
                task_id=task_id,
                runtime_context=runtime_context,
                system_prompt=system_prompt,
                output_contract=output_contract,
            )
            if result is not None and result.value is not None:
                yield str(result.value)
            return

        request = self._build_request(
            user_id=user_id,
            call_site=call_site,
            current_node=current_node,
            current_goal=current_goal,
            current_user_message_id=current_user_message_id,
            task_state_ref=task_state_ref,
            user_content=user_content,
            conversation_id=conversation_id,
            task_id=task_id,
            runtime_context=runtime_context,
            system_prompt=system_prompt,
            output_contract=output_contract,
        )
        async for chunk in invoker_stream(
            request=request,
            llm_task_profile=llm_task_profile,
            runtime_context=runtime_context,
        ):
            yield str(chunk)

    # ── llm_client 兼容接口（供子图直接传入）─────────────────────────

    async def generate_with_profile(
        self,
        llm_task_profile: Any,
        user_content: str,
        *,
        parser: Any = None,
        system_prompt_override: str | None = None,
        **kwargs,
    ) -> "_CompatResult":
        """LLMClient.generate_with_profile 兼容接口。

        使 bridge 可被当作 ``llm_client`` 传入 preparation/repair/incremental
        子图。绑定上下文（user/call_site/runtime_context）由 ``bind()`` 提供。

        §三：Bridge 只表示真实调用成功或失败，不承担 Feature Flag 路由判断。
        路由由调用方在调用前根据 MIG flag 决定（flag=false 走 legacy，不调用本方法；
        flag=true 才调用本方法）。此处 invoker 缺失/invoker 返回 None 视为
        真实失败（error_message="bridge_unavailable"），不是"flag 关闭"信号。
        """
        return await self.bind(
            user_id=0,
            call_site="",
            runtime_context=None,
        ).generate_with_profile(
            llm_task_profile,
            user_content,
            parser=parser,
            system_prompt_override=system_prompt_override,
            **kwargs,
        )


class _CompatResult:
    """LLMProfileResult 兼容形状（parsed/success/error_message）。"""

    __slots__ = ("parsed", "success", "error_message")

    def __init__(self, parsed) -> None:
        self.parsed = parsed
        self.success = parsed is not None
        self.error_message = None if parsed is not None else "bridge_unavailable"


__all__ = ["ContextInvokerBridge", "InvokerBridgeResult"]
