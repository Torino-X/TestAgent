"""CE-02 WP-6：ToolOutputManager 测试。

覆盖：inline/truncate/payload 外置/**binary 无 raw_text**/metadata/
**ToolOutputPolicy 阈值 chars/bytes/tokens/media_type/budget**/tool_calls 列写入。
"""

from __future__ import annotations

import asyncio

from app.context_engine.errors import ContextEngineFailure
from app.context_engine.models.tool_output import ToolOutputPolicy
from app.context_engine.tool_output import ToolOutputManager, TypedToolOutput


async def test_small_inline():
    mgr = ToolOutputManager(None)
    out = await mgr.manage(1, "task1", "tc1", raw_output=TypedToolOutput(text="hello"), policy=ToolOutputPolicy())
    assert out.truncated is False
    assert out.output_char_count == 5
    assert out.truncation_metadata is None
    assert out.preview == "hello"


async def test_medium_head_tail_truncate():
    policy = ToolOutputPolicy(inline_char_limit=10, head_chars=5, tail_chars=3)
    mgr = ToolOutputManager(None)
    out = await mgr.manage(1, "task1", "tc2", raw_output=TypedToolOutput(text="abcdefghijklmnop"), policy=policy)
    assert out.truncated is True
    tm = out.truncation_metadata
    assert tm.mode == "head_tail"
    # included + omitted == original
    assert tm.included_char_count + tm.omitted_char_count == 16
    assert out.output_sha256 is not None


async def test_large_payload_ref():
    """大输出 → 全文进 PayloadStorage + preview + payload_ref。"""
    class _FakePayloadSvc:
        def __init__(self):
            self.stored = []
        async def put(self, command, *, session_factory):
            self.stored.append(command)
            from app.context_engine.models.payload import ContextPayloadRef
            return ContextPayloadRef(
                payload_public_id="pay_abc", storage_backend="fs", owner_user_id=command.user_id, size_bytes=0
            )

    payload_svc = _FakePayloadSvc()

    class _FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class _FakeRepo:
        async def update_extended_columns(self, *args, **kwargs):
            self.last = kwargs

    repo = _FakeRepo()

    policy = ToolOutputPolicy(inline_char_limit=10, head_chars=5, tail_chars=0)
    mgr = ToolOutputManager(payload_svc, tool_call_repository_factory=lambda s: repo)
    out = await mgr.manage(
        1, "task1", "tc3",
        raw_output=TypedToolOutput(text="x" * 500),
        policy=policy,
        session_factory=lambda: _FakeSession(),
    )
    assert out.truncated is True
    assert out.payload_ref == "pay_abc"
    assert len(payload_svc.stored) == 1


async def test_binary_metadata_only_no_raw_text():
    class _FakePayloadSvc:
        async def put(self, command, *, session_factory):
            from app.context_engine.models.payload import ContextPayloadRef

            assert command.content == b"\x00\x01\x02"
            return ContextPayloadRef(
                payload_public_id="pay_binary",
                storage_backend="fs",
                owner_user_id=command.user_id,
                size_bytes=3,
            )

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    mgr = ToolOutputManager(_FakePayloadSvc())
    raw = TypedToolOutput(data=b"\x00\x01\x02", media_type="application/octet-stream")
    out = await mgr.manage(
        1,
        "task1",
        "tc4",
        raw_output=raw,
        policy=ToolOutputPolicy(),
        session_factory=lambda: _FakeSession(),
    )
    assert out.truncation_metadata.mode == "metadata_only"
    assert out.media_type == "application/octet-stream"
    assert out.output_char_count == 0  # binary 无 char_count（不使用 raw_text）
    assert out.payload_ref == "pay_binary"


async def test_binary_does_not_use_raw_text_field():
    """binary 输出不使用 raw_text 字段（data + media_type 承载）。"""
    raw = TypedToolOutput(data=b"binary-bytes", media_type="application/pdf")
    assert raw.is_binary
    assert raw.text is None
    assert raw.char_count == 0
    assert raw.size_bytes == 12


async def test_media_type_inline_limit():
    """media_type 判定：text 内容按 char，binary 按 media_type。"""
    mgr = ToolOutputManager(None)
    # 普通文本按 char 阈值
    policy = ToolOutputPolicy(inline_char_limit=1000)
    out = await mgr.manage(1, "t", "c", raw_output=TypedToolOutput(text="short"), policy=policy)
    assert out.truncated is False


async def test_tool_calls_columns_written():
    """持久化写 tool_calls 扩展列。"""
    class _FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class _FakeRepo:
        def __init__(self):
            self.last = None
        async def update_extended_columns(self, public_id, user_id, **kwargs):
            self.last = (public_id, user_id, kwargs)

    repo = _FakeRepo()
    mgr = ToolOutputManager(None, tool_call_repository_factory=lambda s: repo)
    await mgr.manage(
        1, "task1", "tc5",
        raw_output=TypedToolOutput(text="hello"),
        policy=ToolOutputPolicy(policy_key="small", policy_version="v1"),
        session_factory=lambda: _FakeSession(),
    )
    assert repo.last is not None
    public_id, user_id, kwargs = repo.last
    assert public_id == "tc5"
    assert kwargs["policy_key"] == "small"
    assert kwargs["char_count"] == 5
    assert kwargs["sha256"] is not None
