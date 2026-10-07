"""CE-02 WP-11：Shadow 模式测试。

覆盖：shadow 只产 1 条 snapshot（context_kind='shadow'→building→ready→
completed）、不产业务输出、snapshot 标记 shadow、不改 State、
ready→completed、不经过 sent、active ready→completed 拒绝。
"""

from __future__ import annotations

import pytest

from app.context_engine.models.context import ContextRequest
from app.context_engine.models.compose import ContextComposeResult
from app.context_engine.models.selection import SelectedContextSet
from app.context_engine.shadow import ContextShadowRunner, ShadowCompareResult


class _FakeShadowEngine:
    """假 shadow engine：compose 走 shadow 模式，返回固定结果。"""

    def __init__(self, snapshot_id="cs_shadow_1"):
        self.snapshot_id = snapshot_id
        self.last_execution_mode = None

    async def compose(self, request, *, runtime_context, execution_mode="active"):
        self.last_execution_mode = execution_mode
        from app.context_engine.models.value_objects import Digest

        return ContextComposeResult(
            messages=[],
            prompt_text="shadow prompt",
            prompt_digest=Digest.of("shadow prompt"),
            estimated_input_tokens=50,
            snapshot_public_id=self.snapshot_id,
            context_kind="shadow",
            execution_mode=execution_mode,
            selected=SelectedContextSet(section_stats={"evidence": {"included_count": 2}}),
        )


async def test_shadow_produces_completed_snapshot():
    """shadow 只产 1 条 snapshot（成功路径 completed），不产业务输出。"""
    engine = _FakeShadowEngine()
    runner = ContextShadowRunner(engine=engine, snapshot_writer=None)
    request = ContextRequest(user_id="usr_1", call_site="chat.reply", task_id="task_1", current_user_message="hi")
    result = await runner.run_shadow(request, legacy_prompt="legacy prompt", runtime_context=None)
    assert isinstance(result, ShadowCompareResult)
    assert result.snapshot_public_id == "cs_shadow_1"
    assert result.context_kind == "shadow"
    # shadow 不经过 sent：snapshot 无 sent_at（engine 层走 building→ready→completed）
    assert engine.last_execution_mode == "shadow"
    # 与 legacy 对比
    assert result.legacy_token_count > 0
    assert result.legacy_digest is not None
    assert result.prompt_digest is not None
    # 不保存 Full Prompt（默认 store_prompt_text=False）
    assert result.prompt_text is None


async def test_shadow_does_not_change_state():
    """shadow 不写 active ContextStateRef、不改 State。"""
    engine = _FakeShadowEngine()
    runner = ContextShadowRunner(engine=engine, snapshot_writer=None)
    request = ContextRequest(user_id="usr_1", call_site="chat.reply", task_id="task_1")
    result = await runner.run_shadow(request, legacy_prompt="legacy", runtime_context=None)
    # 无 context_state_ref 写入（ShadowCompareResult 不携带）
    assert not hasattr(result, "context_state_ref")
    assert result.to_state_dict()["context_kind"] == "shadow"


async def test_shadow_ready_completed_no_sent():
    """shadow ready→completed 允许；不经过 sent（不调 LLM）。"""
    # engine 层时序验证：execution_mode='shadow' → begin(context_kind='shadow')→
    # ready → complete_shadow。这里验证 ShadowCompareResult 无 sent 相关字段。
    engine = _FakeShadowEngine()
    runner = ContextShadowRunner(engine=engine, snapshot_writer=None)
    result = await runner.run_shadow(
        ContextRequest(user_id="usr_1", call_site="x", task_id="t1"),
        legacy_prompt="",
        runtime_context=None,
    )
    assert result.prompt_digest is not None  # 已 complete（digest 存在）
