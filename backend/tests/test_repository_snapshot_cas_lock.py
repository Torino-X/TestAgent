"""Phase 2.8A Step 16 — ``update_snapshot_fields`` CAS 路径 3 个测试。

设计目标(对应 docs/29 §10 + §6 + 附录 E 守禁令 #21 上下文):

* 验证 ``update_snapshot_fields`` 的乐观锁 CAS 实现:
  1. 签名锁定 — 参数列表确保 engine_type 永远无法通过这条路径偷改
  2. 版本冲突 → ``rowcount == 0``(caller 可重试或报警)
  3. 版本一致 → ``rowcount == 1``(成功)

为什么这些测试是 Step 16 必需的:
* Step 14 锁死了 ``engine_type`` 不可变;但 ``update_snapshot_fields`` 是
  Phase 2.2 interrupt + checkpointer 用的 CAS 写入路径,也必须不能用同一
  接口偷偷改 ``engine_type``。
* rowcount 语义是上游 ``context_store.py`` 判断"重试还是放弃"的唯一信号 —
  必须区分 rowcount=0(冲突)和 rowcount=1(成功)。

守禁令映射:
* 守禁令 #21 → CAS 冲突时 fail-soft(返回 0 供上游重试),不抛 API 5xx
* 守禁令 #22 → ``update_snapshot_fields`` 签名不接受 ``engine_type``
"""

from __future__ import annotations

import inspect
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


class _FakeExecute:
    """``session.execute`` 的最小替代;通过 ``rowcount`` 切换返回。"""

    def __init__(self, rowcount: int = 1) -> None:
        self.calls: list[tuple[object, dict]] = []
        self._rowcount = rowcount

    def set_rowcount(self, n: int) -> None:
        self._rowcount = n

    async def __call__(self, statement, params=None):
        if hasattr(statement, "text"):
            sql = statement.text
        else:
            sql = str(statement)
        # Insert / Update 同上提取 params
        merged_params = dict(params or {})
        if not merged_params and hasattr(statement, "compile"):
            try:
                compiled = statement.compile()
                merged_params.update(
                    {k: v for k, v in compiled.params.items() if k not in merged_params}
                )
            except Exception:
                pass
        self.calls.append((sql, merged_params))
        result = MagicMock()
        result.rowcount = self._rowcount
        return result


def _make_repo(rowcount: int = 1) -> tuple:
    from app.repositories.agent_task_repository import AgentTaskRepository

    fake_exec = _FakeExecute(rowcount=rowcount)
    fake_session = MagicMock()
    fake_session.execute = fake_exec
    fake_session.flush = AsyncMock()
    repo = AgentTaskRepository(fake_session)
    return repo, fake_session, fake_exec


# ──────────────────────────────────────────────────────────────────────────
# Path 1: 签名锁定 — engine_type 一定不在参数里
# ──────────────────────────────────────────────────────────────────────────


def test_update_snapshot_fields_signature_excludes_engine_type() -> None:
    """``update_snapshot_fields`` 签名排除 ``engine_type``。

    守禁令 #22:``engine_type`` 永远是 immutable 字段,任何写入路径都不得
    接收它作为形参;退役后仓储层不存在任何 engine_type 变更入口。
    """
    from app.repositories.agent_task_repository import AgentTaskRepository

    sig = inspect.signature(AgentTaskRepository.update_snapshot_fields)
    params = list(sig.parameters.keys())
    assert "engine_type" not in params, (
        f"update_snapshot_fields 签名不应包含 engine_type (Phase 2.8A 不可变原则);"
        f"got params={params}"
    )
    # 检查完整签名:self, task_id, snapshot_id, expected_version, current_node,
    # resume_node, now, status(可选)
    expected = {
        "self",
        "task_id",
        "snapshot_id",
        "expected_version",
        "current_node",
        "resume_node",
        "now",
        "status",
    }
    assert expected.issubset(set(params)), (
        f"update_snapshot_fields 必须包含 {expected};got {set(params)}"
    )


# ──────────────────────────────────────────────────────────────────────────
# Path 2: 版本冲突 → rowcount=0(caller 重试)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_snapshot_fields_version_conflict_returns_rowcount_zero() -> None:
    """当 ``context_version`` 被并发任务改掉后,CAS 写不进 → rowcount=0。

    这是守禁令 #21 上游契约的关键:rowcount=0 时 caller (``context_store``)
    选择重试或放弃,**绝不抛错**;生产路径不会因为这个 CAS 失败而 5xx。
    """
    from datetime import timezone

    repo, _session, fake_exec = _make_repo(rowcount=0)

    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    rowcount = await repo.update_snapshot_fields(
        task_id=42,
        snapshot_id=99,
        expected_version=5,  # DB 当前是 6,这条写会被跳过
        current_node="pause_for_legacy_confirm",
        resume_node="resume_section_confirmation",
        now=now,
        status="awaiting_resume",
    )

    assert rowcount == 0
    assert len(fake_exec.calls) == 1
    sql, params = fake_exec.calls[0]
    # SQL 必须显式写 WHERE context_version = :ev
    assert "context_version" in sql.lower()
    assert "context_version = :ev" in sql or ":ev" in sql
    # 参数校验
    assert params["tid"] == 42
    assert params["sid"] == 99
    assert params["ev"] == 5
    assert params["cn"] == "pause_for_legacy_confirm"
    assert params["rn"] == "resume_section_confirmation"
    assert params["status"] == "awaiting_resume"


# ──────────────────────────────────────────────────────────────────────────
# Path 3: 版本一致 → rowcount=1(成功)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_snapshot_fields_version_match_returns_rowcount_one() -> None:
    """当 ``context_version`` 仍等于 ``expected_version`` 时,CAS 写入成功 → rowcount=1。

    SQL 还应让 ``context_version`` 自增 (``= context_version + 1``),
    这样后续任务在写入时会用新 expected_version。CAS 锁的本质。
    """
    from datetime import timezone

    repo, _session, fake_exec = _make_repo(rowcount=1)

    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    rowcount = await repo.update_snapshot_fields(
        task_id=42,
        snapshot_id=100,
        expected_version=6,  # DB 当前也是 6
        current_node="post_confirm_done",
        resume_node=None,
        now=now,
        # 不传 status — 验证 status_clause 动态省略
    )

    assert rowcount == 1
    assert len(fake_exec.calls) == 1
    sql, params = fake_exec.calls[0]
    # SQL 应包含自增 context_version 表达式
    assert "context_version + 1" in sql, (
        f"CAS SQL 必须自增 context_version;got: {sql!r}"
    )
    # 不传 status 时,SQL 里不应包含 status =
    assert "status =" not in sql, (
        f"不传 status 时 SQL 不应有 status =;got: {sql!r}"
    )
    assert "status" not in params


# ──────────────────────────────────────────────────────────────────────────
# Path 4: 反向断言 — params 必须含所有映射 key + snapshot_id/current_node/resume_node
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_snapshot_fields_writes_all_columns_and_param_keys() -> None:
    """回归测试:CAS 路径同时必须包含 snapshot_id / current_node / resume_node /
    checkpoint_updated_at 的 SQL SET 子句与对应 params key(sid/cn/rn/now)。

    这是守禁令反向断言:即使有人把字段从 SET 子句移走,测试也能抓到。
    """
    from datetime import timezone

    repo, _session, fake_exec = _make_repo(rowcount=1)

    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    await repo.update_snapshot_fields(
        task_id=7,
        snapshot_id=42,
        expected_version=3,
        current_node="node_a",
        resume_node="node_b",
        now=now,
    )

    sql, params = fake_exec.calls[0]
    # SQL SET 子句必须列出每列 + params 必须含对应 key
    expected_pairs = (
        ("latest_snapshot_id", "sid"),
        ("current_node", "cn"),
        ("resume_node", "rn"),
        ("checkpoint_updated_at", "now"),
    )
    for col, param_key in expected_pairs:
        assert col in sql, f"CAS SQL 必须 SET {col}; got: {sql!r}"
        assert param_key in params, f"CAS params 必须含 {param_key}(={col})"

    assert params["tid"] == 7
    assert params["ev"] == 3


__all__ = [
    "test_update_snapshot_fields_signature_excludes_engine_type",
    "test_update_snapshot_fields_version_conflict_returns_rowcount_zero",
    "test_update_snapshot_fields_version_match_returns_rowcount_one",
    "test_update_snapshot_fields_writes_all_required_fields",
]
