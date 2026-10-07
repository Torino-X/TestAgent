"""ContextShadowRunner — 独立 Shadow Runner（CE-02 WP-11）。

**不重复调业务 LLM**：只 compose + 生成一条 Snapshot（成功路径，非
abandoned）+ 计算 token/included/dropped/build_latency/prompt_digest；
不写 active ContextStateRef、不改 State。

Shadow 生命周期：``begin(context_kind='shadow', building)`` → attach metadata
→ ``mark_ready`` → ``complete_shadow(ready→completed)``。
字段：``sent_at=null / provider_request_id=null / actual_input_tokens=null /
actual_output_tokens=null / completed_at=当前时间``。不经过 sent、不调 LLM。

CAS：shadow ready→completed 允许；active ready→completed 拒绝；completed
shadow 不可再 sent/fail/abandon。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.compose import ContextComposeResult
from app.context_engine.models.context import ContextRequest
from app.context_engine.models.snapshot_models import ContextSnapshotBeginCommand
from app.context_engine.models.value_objects import Digest


@dataclass(frozen=True, slots=True)
class ShadowCompareResult:
    """Shadow 对比结果。"""

    snapshot_public_id: str
    context_kind: str = "shadow"
    build_latency_ms: int = 0
    token_count: int = 0
    included_count: int = 0
    dropped_count: int = 0
    prompt_digest: str | None = None
    # 与 legacy_prompt 的对比
    legacy_token_count: int = 0
    legacy_digest: str | None = None
    prompt_text: str | None = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "snapshot_public_id": self.snapshot_public_id,
            "context_kind": self.context_kind,
            "build_latency_ms": self.build_latency_ms,
            "token_count": self.token_count,
            "included_count": self.included_count,
            "dropped_count": self.dropped_count,
            "prompt_digest": self.prompt_digest,
            "legacy_token_count": self.legacy_token_count,
            "legacy_digest": self.legacy_digest,
        }


class ContextShadowRunner:
    """Shadow 对比运行器（独立 Runner，不重复调业务 LLM）。"""

    def __init__(
        self,
        *,
        engine,
        snapshot_writer,
        token_counter=None,
        store_prompt_text: bool = False,
    ) -> None:
        self._engine = engine
        self._snapshot_writer = snapshot_writer
        self._token_counter = token_counter
        self._store_prompt_text = store_prompt_text

    async def run_shadow(
        self,
        request: ContextRequest,
        *,
        legacy_prompt: str,
        runtime_context,
    ) -> ShadowCompareResult:
        started = _now_ms()

        # compose（不调业务 LLM）—— execution_mode='shadow' 使 begin 用
        # context_kind='shadow'，并在 _begin_and_ready 后完成 complete_shadow。
        composed = await self._engine.compose(
            request,
            runtime_context=runtime_context,
            execution_mode="shadow",
        )

        # 完成后在 engine 层已走 begin→ready→complete_shadow；此处补齐对比字段
        snapshot_public_id = composed.snapshot_public_id
        if not snapshot_public_id:
            raise_engine_error(
                code="context.shadow.no_snapshot",
                detail="Shadow 未产生 Snapshot",
                stage=ContextEngineStage.SNAPSHOT,
                retryable=False,
                recoverable=True,
            )

        selected = composed.selected
        included_count = len(selected.included) if selected is not None else 0
        dropped_count = len(selected.dropped) if selected is not None else 0

        token_count = composed.estimated_input_tokens
        legacy_token_count = self._estimate(legacy_prompt)
        legacy_digest = str(Digest.of(legacy_prompt)) if legacy_prompt else None

        return ShadowCompareResult(
            snapshot_public_id=snapshot_public_id,
            build_latency_ms=_now_ms() - started,
            token_count=token_count,
            included_count=included_count,
            dropped_count=dropped_count,
            prompt_digest=str(composed.prompt_digest) if composed.prompt_digest else None,
            legacy_token_count=legacy_token_count,
            legacy_digest=legacy_digest,
            prompt_text=composed.prompt_text if self._store_prompt_text else None,
        )

    def _estimate(self, text: str) -> int:
        if not text:
            return 0
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# auto-appended module-level note: shadow runner: 同一输入同时跑新旧引擎, 输出 diff 供回归对比。
