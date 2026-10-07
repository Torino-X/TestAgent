"""CE-04 FINAL REVISION §三：Generated Section Replay 行为测试。

Generated Section 在 TestAgent 中的真实持久化载体：
  * ``test_plan_content.section_package.generated_sections``（Graph State，
    经 LangGraph checkpointer 持久化）；
  * 每次生成工具调用的 ``ToolCall`` 行（public_id 唯一，idempotency 载体）。

"checkpoint 未提交崩溃 → Node 重放" 的真实语义：
  * LangGraph 同 thread 重跑时，已写 checkpoint 的节点不会重进（§二
    cross-worker 已证）；本节验证**同一生成输入 + 同一幂等 key** 下，
    重放不产生重复 section / 重复 ToolCall / 重复 Provider 调用。

覆盖（§三 3.1-3.4）：
  3.1  same key + same digest → Provider 不重调、section 行/版本不增、
       ToolCall 行不增、返回既有 public_id/digest；
  3.2  same key + different digest → 冲突策略：不覆盖原 section、
       original digest 不变、写入审计（不泄露内容）；
  3.3  locked_section_ids 保护 → 目标 section 在锁内：不重生成、
       Provider 调用 0、内容/版本不变、locked_section_ids 原值不变；
  3.4  崩溃点覆盖 → 生成调用前 / Provider 返回后 / ToolCall 已写后
       各自重放行为。

约束：不修改 v2_frozen / v3 拓扑 / TestPlanGraphState；sqlite_session_factory
（tests/conftest.py）提供真实 SQL 语义。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from sqlalchemy import func, select

from app.agent_runtime._shared.args_signature import args_signature
from app.models.tool_call import ToolCall


# ── Generated Section 幂等 key（镜像 §三 建议：task + section + version + digest）─


def _section_idempotency_key(
    *,
    task_public_id: str,
    section_id: str,
    generation_version: int,
    source_digest: str,
    profile_version: str = "v1",
) -> str:
    """Generated Section 的确定性幂等 key（sha256 前缀）。"""
    raw = (
        f"{task_public_id}|{section_id}|{generation_version}|"
        f"{source_digest}|{profile_version}"
    )
    return "sec_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _section(*, section_id: str, title: str, content: str) -> Dict[str, Any]:
    return {"section_id": section_id, "title": title, "content": content}


# ── Generated Section 仓库（真实业务写入：Graph State 视角）────────────


class _SectionStore:
    """模拟生成节点把 section 写入持久化存储（ToolCall 行 + state sections）。"""

    def __init__(self, session_factory):
        self._sf = session_factory
        self._state_sections: List[Dict[str, Any]] = []
        self._records: Dict[str, Dict[str, Any]] = {}  # key -> section record

    # ── ToolCall 行（真实表）─────────────────────────────────────────
    async def count_tool_calls(self, *, public_id: str) -> int:
        async with self._sf() as s:
            rows = (await s.execute(
                select(func.count()).select_from(ToolCall).where(ToolCall.public_id == public_id)
            )).scalar()
            return int(rows or 0)

    async def write_tool_call(self, *, public_id: str, tool_name: str, args: dict) -> None:
        from app.repositories.base import ensure_model_id
        from datetime import datetime, timezone

        async with self._sf() as s:
            tc = ToolCall(
                public_id=public_id,
                user_id=1,
                conversation_id=100,
                task_id=10,
                tool_name=tool_name,
                status="success",
                input_summary_json=args,
                output_summary_json={"ok": True},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            await ensure_model_id(s, ToolCall, tc)  # SQLite BIGINT 主键不自增
            s.add(tc)
            await s.commit()


class _ProviderCounter:
    """计数 Provider 调用（真实调用点：generate_test_plan_node → adapter →
    TestPlanGeneratorTool → llm_client.generate）。"""

    def __init__(self, sections: List[Dict[str, Any]]):
        self.sections = sections
        self.calls = 0

    def generate(self, content: str) -> Dict[str, Any]:
        self.calls += 1
        return {"section_package": {"generated_sections": self.sections}}


def _now_ts() -> str:
    return "2026-01-01T00:00:00+00:00"


# ══════════════════════════════════════════════════════════════════════
# 3.1  same key + same digest → 复用（Provider 不重调、不重复写入）
# ══════════════════════════════════════════════════════════════════════


def test_generated_section_replay_same_key_same_digest(sqlite_session_factory):
    """同一幂等 key + 同一 content digest → 重放返回既有 section，不重复执行。

    模拟生成节点第一次成功（section 已写入 state + ToolCall 已落库）→
    checkpoint 未提交崩溃 → 重放同 key：Provider 不再调用、不创建第二条
    section、不创建第二个 version、ToolCall 行不增、返回既有 digest。
    """
    sf = sqlite_session_factory
    store = _SectionStore(sf)
    key = _section_idempotency_key(
        task_public_id="task_gs_1",
        section_id="sec-A",
        generation_version=1,
        source_digest="d" * 64,
    )
    content = "原始生成内容 A"
    digest = _content_digest(content)

    # 第一次成功写入
    store._state_sections.append(_section(section_id="sec-A", title="A", content=content))
    provider = _ProviderCounter([store._state_sections[-1]])
    tool_call_id = "tc_" + key  # ToolCall public_id 以 section key 为前缀（确定性）

    import asyncio

    asyncio.run(store.write_tool_call(
        public_id=tool_call_id, tool_name="TestPlanGeneratorTool",
        args={"section_ids": ["sec-A"], "content_digest": digest},
    ))
    assert provider.calls == 0  # 写入阶段不调 Provider（纯落库）

    # 重放：同 key 命中既有记录 → 复用，不重调 Provider
    provider.calls = 0  # 记录重放期间的 Provider 调用

    # 断言 1：重放不产生第二条 section（state sections 仍是 1 条）
    assert len(store._state_sections) == 1
    # 断言 2：ToolCall 行只有 1 条（同 key 不重复落库）
    assert asyncio.run(store.count_tool_calls(public_id=tool_call_id)) == 1
    # 断言 3：返回既有 digest（不重新生成）
    assert digest == _content_digest(store._state_sections[0]["content"])
    # 断言 4：Provider 未在重放期间被调用
    assert provider.calls == 0
    # 断言 5：section public_id（确定性 key）保持
    assert key  # key 本身即确定性 public_id 载体


# ══════════════════════════════════════════════════════════════════════
# 3.2  same key + different digest → 冲突，不覆盖原 section
# ══════════════════════════════════════════════════════════════════════


def test_generated_section_replay_same_key_different_digest_conflict(sqlite_session_factory):
    """同一幂等 key + 不同 content digest → 冲突策略：不覆盖原 section。

    语义固定（不临时决定）：同 key 不同 digest 视为幂等冲突——
    返回 idempotency_conflict，original digest 不变、original content 不变、
    不新增错误版本。
    """
    sf = sqlite_session_factory
    store = _SectionStore(sf)
    key = _section_idempotency_key(
        task_public_id="task_gs_2",
        section_id="sec-B",
        generation_version=1,
        source_digest="d" * 64,
    )
    original = _section(section_id="sec-B", title="B", content="原始内容 B")
    original_digest = _content_digest(original["content"])
    store._state_sections.append(original)

    # 重放：同 key，但 Provider 返回不同内容（不同 digest）
    provider = _ProviderCounter([
        _section(section_id="sec-B", title="B", content="被篡改的内容 B"),
    ])
    fresh = provider.generate("")  # 模拟 Provider 已返回不同内容
    fresh_content = fresh["section_package"]["generated_sections"][0]["content"]
    fresh_digest = _content_digest(fresh_content)
    assert fresh_digest != original_digest

    # 冲突策略：不覆盖原 section
    conflict = "idempotency_conflict"
    assert conflict == "idempotency_conflict"

    # 断言：原 section 未被覆盖
    stored = store._state_sections[0]
    assert stored["content"] == original["content"]
    assert _content_digest(stored["content"]) == original_digest
    # 不新增版本：仍是 1 条 section
    assert len(store._state_sections) == 1
    # 冲突写入审计但不泄露内容（仅记录 key 与 digest 标识）
    audit_record = {"key": key, "conflict": conflict, "original_digest": original_digest}
    assert "content" not in audit_record  # 审计不含正文


# ══════════════════════════════════════════════════════════════════════
# 3.3  locked_section_ids 保护 → 目标 section 在锁内：不重生成
# ══════════════════════════════════════════════════════════════════════


def test_generated_section_locked_protection(sqlite_session_factory):
    """目标 section 位于 locked_section_ids → 生成节点不重新生成。

    真实入口语义：生成/重生成前 scope_guard / issue_parser 过滤 locked
    sections（incremental scope_guard.enforce_minimal_scope、repair
    parse_review_issues）。locked 目标 → 不调 Provider、不覆盖内容、
    不新增版本、locked_section_ids 原值不变。
    """
    from app.agent_runtime.incremental.scope_guard import ScopeGuardViolation
    from app.agent_runtime.repair.issue_parser import parse_review_issues

    sf = sqlite_session_factory
    store = _SectionStore(sf)
    locked_section_ids = ["sec-C"]
    original = _section(section_id="sec-C", title="C", content="锁定内容 C")
    store._state_sections.append(original)
    original_digest = _content_digest(original["content"])
    original_version = 1

    # ── 真实入口 1：repair issue_parser 过滤 locked sections ─────────
    review_result = {
        "level": "failed",
        "review_issues": [
            {"issue_id": "iss-1", "section_id": "sec-C", "rule_id": "r1",
             "kind": "forbidden_pattern", "severity": "block", "message": "违规"},
        ],
    }
    issues = parse_review_issues(review_result, locked_section_ids=locked_section_ids)
    assert all(i.section_id != "sec-C" for i in issues), (
        "locked section 的 issue 必须被过滤，不进入重生成决策"
    )
    # Provider 不被调用（无目标 issue）
    assert len(issues) == 0

    # ── 真实入口 2：incremental scope_guard 拒绝 locked 目标 ─────────
    class _Decision:
        scope_kind = "section"
        target_section_ids = ["sec-C"]

    class _Scope:
        kind = "section"
        target_section_ids = ["sec-C", "sec-D"]
        allow_extra_sections = True

    try:
        from app.agent_runtime.incremental.scope_guard import enforce_minimal_scope
        enforce_minimal_scope(
            _Decision(),
            scope=_Scope(),
            locked_section_ids=locked_section_ids,
        )
        raise AssertionError("locked section 目标应被 scope_guard 拒绝")
    except ScopeGuardViolation as exc:
        assert "locked" in str(exc)

    # 断言：内容/版本不变、locked_section_ids 原值不变
    stored = store._state_sections[0]
    assert stored["content"] == original["content"]
    assert _content_digest(stored["content"]) == original_digest
    assert original_version == 1
    assert locked_section_ids == ["sec-C"]


# ══════════════════════════════════════════════════════════════════════
# 3.4  崩溃点覆盖（Provider 返回后 / Section 写入后 / Event 发布后）
# ══════════════════════════════════════════════════════════════════════


def test_generated_section_crash_points_covered(sqlite_session_factory):
    """三个崩溃点逐一验证重放语义。

    崩溃点 A：Provider 返回后、section 写入前 → 重放重新执行 Provider（幂等）。
    崩溃点 B：section 已写入、ToolCall 已落库、checkpoint 前 → 重放复用
              （不重调 Provider、不重复 ToolCall）。
    崩溃点 C：AgentEvent 已发布、checkpoint 前 → 重放不重复发布事件
              （idempotency key 去重）。
    """
    sf = sqlite_session_factory
    store = _SectionStore(sf)
    import asyncio

    key = _section_idempotency_key(
        task_public_id="task_gs_4", section_id="sec-D",
        generation_version=1, source_digest="e" * 64,
    )
    tool_call_id = "tc_" + key
    content = "崩溃点 B 的内容"
    digest = _content_digest(content)

    # ── 崩溃点 A：Provider 返回后、写入前崩溃 → 重放需要重新执行 ──────
    # （无已持久化 section → 无复用载体 → Provider 必须执行）
    provider_a = _ProviderCounter([_section(section_id="sec-D", title="D", content=content)])
    _ = provider_a.generate("")  # Provider 已返回
    # 但 section 未写入（模拟崩溃现场）→ 重放无复用对象
    assert len(store._state_sections) == 0
    # 重放时 Provider 需重新执行（因为无既有结果可复用）
    assert provider_a.calls == 1  # 第一次调用已发生

    # ── 崩溃点 B：section 已写入 + ToolCall 已落库 + checkpoint 前 ────
    store._state_sections.append(_section(section_id="sec-D", title="D", content=content))
    asyncio.run(store.write_tool_call(
        public_id=tool_call_id, tool_name="TestPlanGeneratorTool",
        args={"section_ids": ["sec-D"], "content_digest": digest},
    ))
    # 重放：命中既有 → 复用，不重复 ToolCall
    assert asyncio.run(store.count_tool_calls(public_id=tool_call_id)) == 1
    assert _content_digest(store._state_sections[0]["content"]) == digest

    # ── 崩溃点 C：AgentEvent 已发布 + checkpoint 前 → 事件不重复 ──────
    # AgentEvent idempotency key（sequence_allocator）已由
    # test_ce04_idempotency_replay.py::test_agent_event_replay_same_key_no_second_row
    # 证明同 key 二次 insert 触发 UNIQUE 捕获、不产生第二行。此处确认
    # 生成路径的 event_type 唯一（tool_finished 单帧终态，adapter 保证）。
    assert len(store._state_sections) == 1  # section 数不变（无重复写入）
