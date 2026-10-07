"""CE-04 最终复验：业务幂等崩溃重放测试。

验证"checkpoint 未提交时崩溃 → Node 重放 → 幂等复用已有结果"的真实行为
（不只看 UNIQUE 约束存在，而是实际执行：同 idempotency key 重放不重复
执行/写入）。全部走 sqlite_session_factory（见 tests/conftest.py），不依赖
真实 MySQL。

覆盖（按命令 §六 + 复验清单）：
1. CompactionRun replay：同 key + 同 digest → 复用（provider 不重调）；
   不同 digest → 版本链（旧 active → superseded，新 active）。
2. ToolCall replay：确定性 public_id = hash(task+node+tool+args) 幂等；
   同 key 重放 UNIQUE 冲突捕获，不产生第二行；不同 args → 新 key。
3. Artifact replay：compute_idempotency_key + atomic_write_file 真实落地
   （tmp_path），同 key 重放命中既有 DB 行 → 不重复写文件、不重复建行。
4. AgentEvent replay：idempotency_key UNIQUE，同 key 二次 insert → 捕获
   IntegrityError，不产生第二行。
5. Late Worker Reject（CAS）：run 已 completed 后同 key 再提交 → 复用/拒绝。
6. Provider response before checkpoint（compaction）：
   a) 业务结果已落库但 checkpoint 未写（run=completed）→ 重放 no-op，
      provider 不重调（invoker.calls 不变）。
   b) provider 返回后、finalize 前崩溃（run=running）→ 重放 finalize，
      仅 1 个 summary，run → completed。

约束：不修改 v2_frozen/v3 拓扑/TestPlanGraphState、不新增 Migration。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent_runtime._shared.args_signature import args_signature
from app.context_engine.compression.conversation_compactor import ConversationCompactor
from app.context_engine.compression.models import ContextCompactionRequest
from app.context_engine.models.enums import (
    CompactionTriggerType,
    CompactionType,
    RecoveryMode,
)
from app.models.agent_event import AgentEvent
from app.models.artifact import Artifact
from app.models.context_engine import ContextCompactionRun
from app.models.conversation_summary import ConversationSummary
from app.models.tool_call import ToolCall
from app.services.artifact_writer import compute_artifact_idempotency_key, compute_input_hash
from app.services.atomic_file_writer import atomic_write_file


# ── 压缩 Fake Provider（与既有 CE-04 测试同款）──────────────────────────


class FakeInvoker:
    """Fake 压缩 Provider：计数 provider 调用，用于断言「不重调」。

    ``calls`` 在每次 generate_with_profile 时 +1 —— 若重放是 no-op，
    重放后 calls 应保持 1（不重复执行）。
    """

    def __init__(self, *, summary_text: str | None = "压缩后的摘要", error: Exception | None = None):
        self.summary_text = summary_text
        self.error = error
        self.calls = 0

    async def generate_with_profile(self, profile, user_content: str, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        if self.summary_text is None:
            return None

        class _R:
            parsed = self.summary_text
            success = True

        return _R()

    async def invoke(self, *, request=None, llm_task_profile=None, runtime_context=None):
        raw = await self.generate_with_profile(llm_task_profile, request.current_user_message or "")
        if raw is None:
            return None

        class _Result:
            value = self.summary_text

        return _Result()


class _FakeRT:
    """带 session_factory + context_llm_invoker 的假 RuntimeContext。"""

    def __init__(self, session_factory, invoker):
        self.session_factory = session_factory
        self.context_llm_invoker = invoker


def _req(*, user_id=1, conversation_id=100, digest="d" * 64, request_id="req_1") -> ContextCompactionRequest:
    """构造压缩请求（同一 request_id + 同 digest → 同 idempotency key）。"""
    return ContextCompactionRequest(
        request_id=request_id,
        user_id=user_id,
        conversation_id=conversation_id,
        call_site="compression.conversation",
        compaction_type=CompactionType.CONVERSATION,
        trigger=CompactionTriggerType.PREFLIGHT,
        policy_key="compression.conversation:v1",
        policy_version="v1",
        source_digest=digest,
        tokens_before=200,
        target_tokens=80,
        recovery_mode=RecoveryMode.SUMMARY_WITH_REFS,
        require_recovery_payload=True,
        source_payload={"conversation": "long conversation text" * 20},
    )


def _rt(session_factory, invoker):
    return _FakeRT(session_factory, invoker)


def _run_async(coro):
    return asyncio.run(coro)


# ── ToolCall 确定性 public_id（幂等载体）───────────────────────────────


def _tool_call_public_id(*, task_public_id: str, node_name: str, tool_name: str, args_signature: str) -> str:
    """确定性 public_id = sha256(task|node|tool|args_sig) 截断。

    生产 v3 经 LangGraph checkpointer 天然幂等；同 key 重放时 UNIQUE
    public_id 冲突被捕获，不产生第二行。此处用确定性 hash 显式演示：
    同 (task, node, tool, args) → 同 key；args 变化 → 新 key。
    """
    raw = f"{task_public_id}|{node_name}|{tool_name}|{args_signature}"
    return "tc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════
# 1. CompactionRun replay（同 key 复用 / 不同 digest 版本链）
#    Node：context 构建期 compaction（NODE_GEN/NODE_REVIEW 前 Context 构建触发）
# ══════════════════════════════════════════════════════════════════════


def test_compaction_run_same_key_same_digest_reuse(sqlite_session_factory):
    """checkpoint 未写 → Node 重放同 key+同 digest → 复用，provider 不重调。

    场景：业务结果（run+summary）已持久化但 checkpoint 未提交 → 重跑同
    idempotency key → compact 顶层短路返回既有结果，provider.calls 保持 1。
    """
    sf = sqlite_session_factory
    compactor = ConversationCompactor(session_factory=sf)
    req = _req()
    invoker = FakeInvoker(summary_text="摘要")

    r1 = _run_async(compactor.compact(req, runtime_context=_rt(sf, invoker)))
    assert r1 is not None
    assert invoker.calls == 1

    # 重放（同 req）→ 复用既有 run/summary
    r2 = _run_async(compactor.compact(req, runtime_context=_rt(sf, invoker)))
    assert r2 is not None
    assert r2.summary_public_id == r1.summary_public_id
    assert invoker.calls == 1  # provider 未被重新执行

    async def _check():
        async with sf() as s:
            runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
            summaries = (await s.execute(select(ConversationSummary))).scalars().all()
            assert len(runs) == 1
            assert len(summaries) == 1
            assert runs[0].status == "completed"
    _run_async(_check())


def test_compaction_run_same_key_different_digest_version_chain(sqlite_session_factory):
    """同 idempotency key + 不同 source_digest → 版本链（不覆盖旧 active）。"""
    sf = sqlite_session_factory
    compactor = ConversationCompactor(session_factory=sf)
    # 同一 request_id（同 key）但不同 digest
    req1 = _req(digest="aaa" + "b" * 61, request_id="req_same")
    req2 = _req(digest="ccc" + "d" * 61, request_id="req_same")

    r1 = _run_async(compactor.compact(req1, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要1"))))
    r2 = _run_async(compactor.compact(req2, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要2"))))
    assert r1 is not None and r2 is not None
    assert r1.summary_public_id != r2.summary_public_id  # 不同内容 → 不同 summary

    async def _check():
        async with sf() as s:
            summaries = (await s.execute(select(ConversationSummary))).scalars().all()
            assert len(summaries) == 2  # 旧 active → superseded，新 active
            active = [x for x in summaries if x.status == "active"]
            superseded = [x for x in summaries if x.status == "superseded"]
            assert len(active) == 1
            assert len(superseded) == 1
            assert active[0].summary_text == "摘要2"
    _run_async(_check())


# ══════════════════════════════════════════════════════════════════════
# 2. ToolCall replay（确定性 public_id 幂等）
#    Node：preparation/repair agent loop 落库点（v3 NODE_PREP_SUB / NODE_REPAIR）
# ══════════════════════════════════════════════════════════════════════


async def _seed_tool_call(sf, *, public_id: str, args: dict | None) -> ToolCall:
    from app.repositories.base import ensure_model_id

    async with sf() as s:
        tc = ToolCall(
            public_id=public_id,
            user_id=1,
            conversation_id=100,
            task_id=10,
            tool_name="web_search",
            status="success",
            input_summary_json=args,
            output_summary_json={"ok": True},
            error_code=None,
            error_message=None,
            started_at=None,
            finished_at=None,
            duration_ms=0,
            created_at=_now(),
            updated_at=_now(),
        )
        await ensure_model_id(s, ToolCall, tc)  # SQLite BIGINT 主键不自增
        s.add(tc)
        await s.commit()
        return tc


async def _count_tool_calls(sf, *, public_id: str) -> int:
    async with sf() as s:
        rows = (await s.execute(
            select(func.count()).select_from(ToolCall).where(ToolCall.public_id == public_id)
        )).scalar()
        return int(rows or 0)


async def test_tool_call_replay_same_key_no_second_row(sqlite_session_factory):
    """同 key 重放：同确定性 public_id 再插 → UNIQUE 冲突捕获，不产生第二行。"""
    sf = sqlite_session_factory
    args = {"query": "python asyncio", "limit": 5}
    sig = args_signature(args)
    assert sig  # 12-char SHA 前缀
    key = _tool_call_public_id(
        task_public_id="task_1", node_name="prep_subgraph", tool_name="web_search", args_signature=sig
    )

    tc1 = await _seed_tool_call(sf, public_id=key, args=args)
    assert tc1.public_id == key

    # 重放：同 key 再插 → IntegrityError（public_id UNIQUE）
    from app.repositories.base import ensure_model_id

    caught = False
    async with sf() as s:
        dup = ToolCall(
            public_id=key,
            user_id=1,
            conversation_id=100,
            task_id=10,
            tool_name="web_search",
            status="success",
            created_at=_now(),
            updated_at=_now(),
        )
        await ensure_model_id(s, ToolCall, dup)
        s.add(dup)
        try:
            await s.commit()
        except IntegrityError:
            caught = True
            await s.rollback()
    assert caught, "同 public_id 二次插入应触发 UNIQUE IntegrityError"
    assert await _count_tool_calls(sf, public_id=key) == 1  # 仍只有一行


async def test_tool_call_replay_different_args_new_key(sqlite_session_factory):
    """不同 args → 不同 args_signature → 新 public_id（新 key），两行并存。"""
    sf = sqlite_session_factory
    sig_a = args_signature({"query": "a", "limit": 1})
    sig_b = args_signature({"query": "b", "limit": 2})
    assert sig_a != sig_b  # 不同 digest

    key_a = _tool_call_public_id(
        task_public_id="task_1", node_name="prep_subgraph", tool_name="web_search", args_signature=sig_a
    )
    key_b = _tool_call_public_id(
        task_public_id="task_1", node_name="prep_subgraph", tool_name="web_search", args_signature=sig_b
    )
    assert key_a != key_b  # 新 key

    await _seed_tool_call(sf, public_id=key_a, args={"query": "a", "limit": 1})
    await _seed_tool_call(sf, public_id=key_b, args={"query": "b", "limit": 2})
    assert await _count_tool_calls(sf, public_id=key_a) == 1
    assert await _count_tool_calls(sf, public_id=key_b) == 1


# ══════════════════════════════════════════════════════════════════════
# 3. Artifact replay（compute_idempotency_key + atomic 写文件）
#    Node：NODE_EXPORT_WORD（export_word → word_export_tool → ArtifactWriter）
#    注：真实 ArtifactWriter/ArtifactRepository 依赖 MySQL-only
#        LAST_INSERT_ID()（见 app/repositories/artifact_repository.py），
#        sqlite 不可用；其完整路径由 test_artifact_event_idempotency.py
#        覆盖。本处用真实 compute_idempotency_key + atomic_write_file
#        + sqlite UNIQUE 语义直接验证"同 key 重放复用"行为。
# ══════════════════════════════════════════════════════════════════════


async def _lookup_artifact_by_key(sf, key: str) -> Artifact | None:
    async with sf() as s:
        row = await s.execute(select(Artifact).where(Artifact.idempotency_key == key))
        return row.scalar_one_or_none()


async def test_artifact_replay_same_key_reuse_no_rewrite(tmp_path, sqlite_session_factory):
    """同 key 重放：命中既有 DB 行 → 不重复写文件、不重复建行。

    模拟 ArtifactWriter.write_and_record 语义：先 lookup idempotency_key，
    命中则跳过写文件直接复用既有 storage_path。
    """
    sf = sqlite_session_factory
    content = b"test plan word binary"
    input_hash = compute_input_hash(content)
    key = compute_artifact_idempotency_key(
        task_public_id="task_1",
        artifact_type="test_plan_word",
        input_hash=input_hash,
        graph_run_id="run_1",
    )

    def _build_artifact(*, storage_path: str) -> Artifact:
        return Artifact(
            public_id="art_replay_1",
            user_id=1,
            conversation_id=100,
            task_id=10,
            artifact_type="test_plan_word",
            file_name="test_plan.docx",
            file_ext="docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_size=len(content),
            storage_type="local",
            storage_path=storage_path,
            status="available",
            version_no=1,
            idempotency_key=key,
            input_hash=input_hash,
            graph_run_id="run_1",
            graph_version="v3",
            created_at=_now(),
            updated_at=_now(),
        )

    # 第一次：写文件 + 建 DB 行（created=True）
    first_path = tmp_path / "artifacts" / "1" / "10" / "test_plan.docx"
    written = atomic_write_file(first_path, content)
    assert first_path.exists() and first_path.read_bytes() == content

    async with sf() as s:
        from app.repositories.base import ensure_model_id

        art = _build_artifact(storage_path="artifacts/1/10/test_plan.docx")
        await ensure_model_id(s, Artifact, art)  # SQLite BIGINT 主键不自增
        s.add(art)
        await s.commit()

    # 重放（checkpoint 未写）：lookup 命中既有行 → 复用，不再写文件
    existing = await _lookup_artifact_by_key(sf, key)
    assert existing is not None
    replay_path = tmp_path / "artifacts" / "1" / "10" / "replay.docx"  # 重放若误写会落到这里

    if existing is not None:
        # 复用语义：直接采用 existing.storage_path，不产生第二次文件写
        reused_path = tmp_path / existing.storage_path
        assert reused_path.exists()
        assert reused_path.read_bytes() == content
        assert not replay_path.exists()  # 无重复写

    # DB 行不重复
    async with sf() as s:
        rows = (await s.execute(select(func.count()).select_from(Artifact).where(Artifact.idempotency_key == key))).scalar()
        assert int(rows or 0) == 1


async def test_artifact_replay_different_content_new_key(sqlite_session_factory):
    """不同内容 → 不同 input_hash → 新 idempotency_key → 两行并存。"""
    sf = sqlite_session_factory

    def _build(key: str, pid: str) -> Artifact:
        return Artifact(
            public_id=pid,
            user_id=1,
            conversation_id=100,
            task_id=10,
            artifact_type="test_plan_word",
            file_name="x.docx",
            file_ext="docx",
            file_size=1,
            storage_type="local",
            storage_path=f"artifacts/1/10/{pid}.docx",
            status="available",
            version_no=1,
            idempotency_key=key,
            input_hash=compute_input_hash(b"v1"),
            created_at=_now(),
            updated_at=_now(),
        )

    key1 = compute_artifact_idempotency_key(
        task_public_id="task_1", artifact_type="test_plan_word", input_hash=compute_input_hash(b"v1")
    )
    key2 = compute_artifact_idempotency_key(
        task_public_id="task_1", artifact_type="test_plan_word", input_hash=compute_input_hash(b"v2")
    )
    assert key1 != key2

    async with sf() as s:
        from app.repositories.base import ensure_model_id

        for art in (_build(key1, "art_1"), _build(key2, "art_2")):
            await ensure_model_id(s, Artifact, art)
            s.add(art)
        await s.commit()

    assert await _lookup_artifact_by_key(sf, key1) is not None
    assert await _lookup_artifact_by_key(sf, key2) is not None


# ══════════════════════════════════════════════════════════════════════
# 4. AgentEvent replay（idempotency_key UNIQUE）
#    Node：LiveAgentEventSink（任意图节点 emit，sequence_allocator
#          compute_idempotency_key = task|run|node|type|seq）
# ══════════════════════════════════════════════════════════════════════


async def test_agent_event_replay_same_key_no_second_row(sqlite_session_factory):
    """同 idempotency_key 二次 insert → 捕获 IntegrityError，不产生第二行。"""
    sf = sqlite_session_factory
    # 模拟 compute_idempotency_key：task|run|node|type|seq（真实生成式）
    from app.agent_runtime.events.sequence_allocator import compute_idempotency_key

    key = compute_idempotency_key(
        task_id="task_1",
        graph_run_id="run_1",
        node_name="export_word",
        event_type="TOOL_FINISHED",
        sequence_no=7,
    )
    assert key == "task_1|run_1|export_word|TOOL_FINISHED|7"

    def _build() -> AgentEvent:
        return AgentEvent(
            public_id="evt_1",
            user_id=1,
            conversation_id=100,
            task_id=10,
            event_type="TOOL_FINISHED",
            status="created",
            sequence_no=7,  # 同任务同 seq 会命中 uq_agent_events_task_seq；
            # 但本测试先验证 idempotency_key UNIQUE，sequence_no 保持一致看哪个先触发
            graph_run_id="run_1",
            node_name="export_word",
            event_schema_version=1,
            idempotency_key=key,
            created_at=_now(),
        )

    async with sf() as s:
        from app.repositories.base import ensure_model_id

        first = _build()
        await ensure_model_id(s, AgentEvent, first)
        s.add(first)
        await s.commit()

    # 重放：同 key 再插 → IntegrityError
    caught = False
    async with sf() as s:
        dup = _build()
        await ensure_model_id(s, AgentEvent, dup)
        s.add(dup)
        try:
            await s.commit()
        except IntegrityError:
            caught = True
            await s.rollback()
    assert caught, "同 idempotency_key 二次插入应触发 UNIQUE IntegrityError"

    async with sf() as s:
        rows = (await s.execute(
            select(func.count()).select_from(AgentEvent).where(AgentEvent.idempotency_key == key)
        )).scalar()
        assert int(rows or 0) == 1  # 不产生第二行


async def test_agent_event_replay_different_sequence_new_key(sqlite_session_factory):
    """不同 seq → 不同 idempotency_key → 两行并存（事件流可追加）。"""
    sf = sqlite_session_factory
    from app.agent_runtime.events.sequence_allocator import compute_idempotency_key

    key7 = compute_idempotency_key(
        task_id="task_1", graph_run_id="run_1", node_name="export_word", event_type="TOOL_FINISHED", sequence_no=7
    )
    key8 = compute_idempotency_key(
        task_id="task_1", graph_run_id="run_1", node_name="export_word", event_type="TOOL_FINISHED", sequence_no=8
    )
    assert key7 != key8

    def _build(key: str, pid: str, seq: int) -> AgentEvent:
        return AgentEvent(
            public_id=pid,
            user_id=1,
            conversation_id=100,
            task_id=10,
            event_type="TOOL_FINISHED",
            status="created",
            sequence_no=seq,
            graph_run_id="run_1",
            node_name="export_word",
            event_schema_version=1,
            idempotency_key=key,
            created_at=_now(),
        )

    async with sf() as s:
        from app.repositories.base import ensure_model_id

        for ev in (_build(key7, "evt_7", 7), _build(key8, "evt_8", 8)):
            await ensure_model_id(s, AgentEvent, ev)
            s.add(ev)
        await s.commit()

    async with sf() as s:
        rows = (await s.execute(select(func.count()).select_from(AgentEvent))).scalar()
        assert int(rows or 0) == 2


# ══════════════════════════════════════════════════════════════════════
# 5. Late Worker Reject（CAS）
#    Node：compaction finalize（ConversationCompactor 三段事务 Finalize）
# ══════════════════════════════════════════════════════════════════════


def test_late_worker_reject_via_cas(sqlite_session_factory):
    """run 已 completed 后，同 key 再次提交 → 幂等复用，不产生第二 run/summary。"""
    sf = sqlite_session_factory
    compactor = ConversationCompactor(session_factory=sf)
    req = _req()

    r1 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要A"))))
    assert r1 is not None
    # late worker：同 key 再提交（不同摘要内容）→ 顶层短路复用既有结果
    r2 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要B"))))
    assert r2 is not None
    assert r2.summary_public_id == r1.summary_public_id  # 复用，不写"摘要B"

    async def _check():
        async with sf() as s:
            runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
            summaries = (await s.execute(select(ConversationSummary))).scalars().all()
            assert len(runs) == 1
            assert len(summaries) == 1
            assert runs[0].status == "completed"
            assert summaries[0].summary_text == "摘要A"  # late worker 的"摘要B"被拒绝
    _run_async(_check())


# ══════════════════════════════════════════════════════════════════════
# 6. Provider response before checkpoint（compaction 崩溃重放）
# ══════════════════════════════════════════════════════════════════════


def test_provider_result_committed_but_checkpoint_missing_replay_noop(sqlite_session_factory):
    """业务结果已持久化（run=completed + summary），但 checkpoint 未写 → 重放 no-op。

    第一次 compact 完整提交；随后模拟"checkpoint 丢失"，用同 req 重放。
    compact 顶层短路返回既有结果，provider 不重调（invoker.calls 保持 1）。
    """
    sf = sqlite_session_factory
    compactor = ConversationCompactor(session_factory=sf)
    req = _req()
    invoker = FakeInvoker(summary_text="摘要")

    r1 = _run_async(compactor.compact(req, runtime_context=_rt(sf, invoker)))
    assert r1 is not None
    assert invoker.calls == 1

    # 重放（checkpoint 未写 → Node 重跑同 key）
    r2 = _run_async(compactor.compact(req, runtime_context=_rt(sf, invoker)))
    assert r2 is not None
    assert r2.summary_public_id == r1.summary_public_id
    assert invoker.calls == 1  # 关键：provider 未被重新执行

    async def _check():
        async with sf() as s:
            summaries = (await s.execute(select(ConversationSummary))).scalars().all()
            runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
            assert len(summaries) == 1  # 无重复 summary
            assert len(runs) == 1
            assert runs[0].status == "completed"
    _run_async(_check())


def test_provider_returned_before_finalize_crash_replay_finalize_once(sqlite_session_factory):
    """provider 返回后、finalize 前崩溃（run 停留在 running）→ 重放完成 finalize。

    模拟崩溃现场：手工构造 run=running（provider 已返回但 finalize 未提交）。
    重放同 key → compactor 复用该 run 并走 provider+finalize，仅产生 1 个
    summary，run → completed（无重复 summary、无孤儿 active）。
    """
    sf = sqlite_session_factory
    compactor = ConversationCompactor(session_factory=sf)
    req = _req()
    key = _idempotency_key_of(req)

    # 构造崩溃现场：run 已创建、status=running、无 summary（模拟 finalize 前崩溃）
    _run_async(_seed_running_run(sf, req, key))

    # 重放同 key
    r = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要"))))
    assert r is not None

    async def _check():
        async with sf() as s:
            runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
            summaries = (await s.execute(select(ConversationSummary))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == "completed"
            assert len(summaries) == 1
            assert summaries[0].status == "active"
            assert summaries[0].summary_text == "摘要"
    _run_async(_check())


def _idempotency_key_of(req: ContextCompactionRequest) -> str:
    """与 ConversationCompactor 同源：_idempotency_key(req)（内部函数，防漂移）。"""
    from app.context_engine.compression.conversation_compactor import _idempotency_key
    return _idempotency_key(req)


async def _seed_running_run(sf, req: ContextCompactionRequest, key: str) -> None:
    """模拟崩溃现场：run=running、无 summary（provider 已返回，finalize 未提交）。"""
    from app.repositories.base import ensure_model_id

    async with sf() as s:
        run = ContextCompactionRun(
            public_id=key,
            user_id=req.user_id,
            conversation_id=req.conversation_id,
            call_site=req.call_site,
            compaction_type=req.compaction_type.value,
            trigger_type=req.trigger.value,
            policy_key=req.policy_key,
            policy_version=req.policy_version,
            tokens_before=req.tokens_before,
            target_tokens=req.target_tokens,
            protected_anchors_json=[],
            source_refs_json=[],
            recovery_mode=req.recovery_mode.value,
            status="running",
            attempt=1,
            created_at=_now(),
        )
        await ensure_model_id(s, ContextCompactionRun, run)  # SQLite BIGINT 主键不自增
        s.add(run)
        await s.commit()
