"""TaskAttachmentResolver._wait_for_profiles_ready 回归测试。

BUG FIX 2026-08-19（方案 A）：用户上传完立刻发起任务，FileSemanticProfile
通常还在 processing；resolver 不等就0 分导致任务被拒。本文件覆盖等待
逻辑的4 个关键场景：

  1. profile 立即 ready  → 不等待，返回原 profiles
  2. profile 从 processing → ready（在预算内） → 等到 ready 后返回
  3. profile 终态失败（failed / unsupported）→ 不等，直接返回
  4. profile 始终未 ready → 5s 后超时返回当前快照

不接 DB，用 MagicMock 模拟 AsyncSession.execute/scalars。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.task_attachment_resolver import (
    PROFILE_READY_POLL_INTERVAL_S,
    PROFILE_READY_WAIT_TIMEOUT_S,
    TaskAttachmentResolver,
)


def _make_session_with_profiles(profiles_by_id: dict[int, SimpleNamespace]) -> MagicMock:
    """构造一个 mock AsyncSession，execute 返回 profiles_by_id。"""
    session = MagicMock()

    async def _execute(_stmt):
        # 不解析 stmt，直接按 file_id 过滤返回 profiles
        wanted_ids = set()
        bind_params = getattr(_stmt, "_bindparams", None)  # noqa: F841
        # 简化：从 profiles_by_id 全量返回（_wait_for_profiles_ready 内部
        # 仅重拉未 ready 的 file_id，测试 mock 时按 file_id in_(...) 返
        # 回等价于完整集合即可）。
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(profiles_by_id.values())
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _profile(file_id: int, status: str, **kwargs) -> SimpleNamespace:
    p = SimpleNamespace(
        file_id=file_id,
        status=status,
        document_kind=kwargs.get("document_kind", "unknown"),
        possible_usages_json=kwargs.get("possible_usages_json", []),
        confidence=kwargs.get("confidence", 0.0),
    )
    return p


def _file(file_id: int, public_id: str = "f_x", file_ext: str = ".docx") -> SimpleNamespace:
    return SimpleNamespace(
        id=file_id,
        public_id=public_id,
        original_name=f"{public_id}.docx",
        file_ext=file_ext,
        file_type="unknown",
    )


@pytest.mark.asyncio
async def test_profile_already_ready_returns_immediately(monkeypatch):
    """所有 profile 一开始就是 ready → _wait_for_profiles_ready 不应循环等待。"""
    profiles = {1: _profile(1, "ready"), 2: _profile(2, "ready")}
    session = _make_session_with_profiles(profiles)
    resolver = TaskAttachmentResolver(session)

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _tracked_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)  # 真实 yield 不阻塞

    monkeypatch.setattr(asyncio, "sleep", _tracked_sleep)

    files = [_file(1), _file(2)]
    result = await resolver._wait_for_profiles_ready(files)

    assert set(result.keys()) == {1, 2}
    # profile 全部 ready，while 循环第一轮就 break → 0 次 sleep
    assert sleeps == []


@pytest.mark.asyncio
async def test_profile_becomes_ready_within_budget(monkeypatch):
    """profile 从 processing → ready（在预算内）：等到 ready 返回。"""
    # 模拟 profile 状态机：第1 次 _profiles_by_file_id 返 processing，
    # 第2 次返 ready。
    call_count = {"n": 0}

    session = MagicMock()

    async def _execute(_stmt):
        call_count["n"] += 1
        profiles_now = (
            [_profile(1, "ready"), _profile(2, "ready")]
            if call_count["n"] >= 2
            else [_profile(1, "processing"), _profile(2, "processing")]
        )
        result = MagicMock()
        result.scalars.return_value.all.return_value = profiles_now
        return result

    session.execute = AsyncMock(side_effect=_execute)
    resolver = TaskAttachmentResolver(session)

    # 把 poll 间隔压到 0 → 测试不依赖真实 sleep
    monkeypatch.setattr(
        "app.services.task_attachment_resolver.PROFILE_READY_POLL_INTERVAL_S", 0
    )

    files = [_file(1), _file(2)]
    result = await resolver._wait_for_profiles_ready(files)

    assert result[1].status == "ready"
    assert result[2].status == "ready"
    # 第1 轮都是 processing → 重拉；第2 轮都是 ready → break
    assert call_count["n"] >= 2


@pytest.mark.asyncio
async def test_terminal_failed_status_returns_without_waiting(monkeypatch):
    """profile 已是 failed / unsupported（终态失败）→ 不再等。"""
    profiles = {1: _profile(1, "ready"), 2: _profile(2, "failed")}
    session = _make_session_with_profiles(profiles)
    resolver = TaskAttachmentResolver(session)

    sleeps: list[float] = []

    async def _tracked_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _tracked_sleep)

    files = [_file(1), _file(2)]
    result = await resolver._wait_for_profiles_ready(files)

    assert result[1].status == "ready"
    assert result[2].status == "failed"
    # 1 ready + 1 failed → pending_ids 为空 → 直接 break
    assert sleeps == []


@pytest.mark.asyncio
async def test_profile_never_ready_times_out(monkeypatch):
    """profile 始终 processing → 5s 后超时返回当前快照。"""
    profiles = {1: _profile(1, "processing")}
    session = _make_session_with_profiles(profiles)
    resolver = TaskAttachmentResolver(session)

    # 把超时预算压回 0.05s、间隔 0 → 测试快速
    monkeypatch.setattr(
        "app.services.task_attachment_resolver.PROFILE_READY_WAIT_TIMEOUT_S", 0.05
    )
    monkeypatch.setattr(
        "app.services.task_attachment_resolver.PROFILE_READY_POLL_INTERVAL_S", 0
    )

    files = [_file(1)]
    result = await resolver._wait_for_profiles_ready(files)

    # 超时后返回当前快照（仍是 processing）
    assert result[1].status == "processing"


@pytest.mark.asyncio
async def test_missing_profile_returns_empty(monkeypatch):
    """文件没有 profile 记录 → 返回空 dict（与旧 _profiles_by_file_id 一致）。"""
    session = _make_session_with_profiles({})
    resolver = TaskAttachmentResolver(session)

    files = [_file(1)]
    result = await resolver._wait_for_profiles_ready(files)
    assert result == {}


def test_constants_have_reasonable_defaults():
    """常量值应为合理默认（避免被静默改小）。"""
    assert 3.0 <= PROFILE_READY_WAIT_TIMEOUT_S <= 10.0
    assert 0.05 <= PROFILE_READY_POLL_INTERVAL_S <= 0.5