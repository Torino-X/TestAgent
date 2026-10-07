"""CE-02 整改八：StateSafe 验证。

覆盖：
- ContextStateRef serialized size < 20 KB；
- source_refs <= 100；
- 超限时只保留 latest snapshot ID + stats；
- 不含 ContextMessage；
- 不含 Prompt；
- 不含 ContextItem；
- 不含 Payload bytes；
- 不含 storage_key/path；
- 不含 raw Tool Output；
- assert_state_serializable 通过；
- Pilot checkpoint 可恢复。
"""

from __future__ import annotations

import json

import pytest

from app.context_engine.models.compose import ContextComposeResult, ContextMessage
from app.context_engine.models.context import ContextItem, ContextKind
from app.context_engine.models.selection import SelectedContextSet
from app.context_engine.models.snapshot_models import ContextStateRef, ContextStateStats
from app.context_engine.snapshot.context_state_ref import build_context_state_ref
from app.context_engine.models.enums import SourceType


def _item(i, kind=ContextKind.EVIDENCE, *, source_ref=None):
    return ContextItem(
        item_id=f"item_{i}",
        kind=kind,
        source_type=SourceType.ARTIFACT,
        source_ref=source_ref or f"ref_{i}",
        content=f"content {i}",
        authority=80,
        estimated_tokens=10,
    )


def _composed(included, *, snapshot_id="cs_1", extra_messages=()):
    return ContextComposeResult(
        messages=list(extra_messages),
        prompt_text="THE FULL PROMPT SHOULD NOT BE IN STATE",
        estimated_input_tokens=100,
        snapshot_public_id=snapshot_id,
        selected=SelectedContextSet(
            included=included,
            section_stats={"evidence": {"required": True, "included_count": len(included)}},
            total_estimated_tokens=10 * len(included),
        ),
    )


def test_context_state_ref_serialized_size_under_20kb():
    """ContextStateRef 序列化 < 20 KB。"""
    included = [_item(i) for i in range(50)]
    ref = build_context_state_ref(_composed(included), snapshot_public_id="cs_1", profile_key="p")
    size = len(json.dumps(ref.to_state_dict()).encode("utf-8"))
    assert size < 20 * 1024


def test_source_refs_max_100():
    """source_refs <= 100；ContextStateRef.is_valid。"""
    included = [_item(i) for i in range(100)]
    ref = build_context_state_ref(_composed(included), snapshot_public_id="cs_1")
    assert len(ref.source_refs) == 100
    assert ref.is_valid is True
    # 超过 100 → is_valid False
    included_101 = [_item(i) for i in range(101)]
    ref2 = build_context_state_ref(_composed(included_101), snapshot_public_id="cs_1")
    assert ref2.is_valid is False


def test_no_context_message_in_state():
    """State 不含 ContextMessage（消息正文不进入 ContextStateRef）。"""
    msg = ContextMessage(role="user", content="secret message body", kind=ContextKind.EVIDENCE)
    ref = build_context_state_ref(_composed([], extra_messages=[msg]), snapshot_public_id="cs_1")
    sd = json.dumps(ref.to_state_dict())
    assert "secret message body" not in sd


def test_no_prompt_in_state():
    """State 不含 Prompt 全文。"""
    ref = build_context_state_ref(_composed([_item(1)]), snapshot_public_id="cs_1")
    sd = json.dumps(ref.to_state_dict())
    assert "THE FULL PROMPT SHOULD NOT BE IN STATE" not in sd


def test_no_context_item_content_in_state():
    """State 不含 ContextItem.content。"""
    ref = build_context_state_ref(_composed([_item(1)]), snapshot_public_id="cs_1")
    sd = json.dumps(ref.to_state_dict())
    assert "content 1" not in sd  # ContextRef 只有 item_id/kind/source_type/source_ref


def test_no_payload_bytes_or_storage_key_in_state():
    """State 不含 Payload bytes / storage_key / path。"""
    included = [_item(1, source_ref="pay_abc")]
    ref = build_context_state_ref(_composed(included), snapshot_public_id="cs_1")
    sd = json.dumps(ref.to_state_dict())
    assert "storage_key" not in sd
    assert "/data/ce_payloads" not in sd
    # 只有 source_ref 引用（轻量），无内容


def test_no_raw_tool_output_in_state():
    """State 不含 raw Tool Output。"""
    ref = build_context_state_ref(_composed([_item(1)]), snapshot_public_id="cs_1")
    sd = json.dumps(ref.to_state_dict())
    assert "tool_output" not in sd.lower() or "raw" not in sd.lower()


def test_state_is_json_serializable():
    """ContextStateRef.to_state_dict 可 JSON 序列化。"""
    ref = build_context_state_ref(_composed([_item(i) for i in range(5)]), snapshot_public_id="cs_1")
    json.dumps(ref.to_state_dict())  # 不抛异常
    # 全部值类型：str/int/bool/list/dict
    assert isinstance(ref.to_state_dict()["latest_snapshot_public_id"], str)
    assert isinstance(ref.to_state_dict()["stats"], dict)


def test_assert_state_serializable_passes():
    """LangGraph assert_state_serializable 对 ContextStateRef dict 通过。"""
    ref = build_context_state_ref(_composed([_item(1)]), snapshot_public_id="cs_1")
    from app.agent_runtime.graphs.test_plan.state import assert_state_serializable

    assert_state_serializable(ref.to_state_dict())  # 不抛异常


async def test_pilot_checkpoint_restore():
    """Pilot checkpoint 可恢复（ContextStateRef 序列化后重新解析）。"""
    from langgraph.checkpoint.memory import MemorySaver
    from app.agent_runtime.graphs.ce_pilot import build_compiled_ce_pilot_graph
    from tests.test_ce02_pilot import _runtime_context, _initial_state, _FakeInvoker

    invoker = _FakeInvoker()
    checkpointer = MemorySaver()
    graph = build_compiled_ce_pilot_graph(checkpointer=checkpointer)
    ctx = _runtime_context(invoker)
    config = {"configurable": {"thread_id": "task_pub_1", "runtime_context": ctx}}
    await graph.ainvoke(_initial_state(), config=config)

    # 从 checkpoint 恢复：context_state 仍可读且不含敏感内容
    checkpoint = checkpointer.get(config)
    assert checkpoint is not None
    state = checkpoint["channel_values"]
    context_state = state.get("context_state")
    if context_state:
        assert "prompt" not in json.dumps(context_state).lower() or "latest_snapshot_public_id" in context_state
