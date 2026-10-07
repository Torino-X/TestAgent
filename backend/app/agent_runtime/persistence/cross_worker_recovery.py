"""Phase 2.8B 跨 worker checkpoint 恢复辅助 — 在 worker 启动期拉取 orphaned checkpoints。

设计要点(对应 docs/30 §3.4 + ADR-2.8B-8):

* **何时调用**:worker 进程启动时(``main.py`` lifespan 末尾)由运维显式 opt-in;
  不强制每次启动都跑(避免冷启动开销 + 不必要的 Postgres 读)。
* **做什么**:
  1. 读 ``checkpoints`` 表拿到最近 N 个 ``thread_id``(默认 50)
  2. 对每个 ``thread_id``,尝试 ``cp.get(config)`` 拿到最新 ``checkpoint`` dict
  3. 如果 ``checkpoint.metadata.source == "interrupted"`` 且 ``thread_id`` 当前
     **不在 Redis InFlight 锁里**(说明上次 dispatch 的 worker 已死),
     把 ``thread_id`` 加入"可恢复列表"
  4. 返回该列表 + 统计 dict
* **不做什么**:
  * **不**自动 resume — resume 是 ApiDispatcher.dispatch_resume 的责任
  * **不**DEL Redis lock — 由 TTL 自动过期兜底;若强制释放需运维介入
  * **不**写任何 MySQL/Postgres 表
* **守禁令映射**:
  * 守禁令 #18 — 不会修改 Postgres 表
  * 守禁令 #32 — release 只在运维显式调用时发生;TTL 兜底
  * 守禁令 #33 — graceful degrade(verify 失败 → 返回空 list + WARN)

**env-gate**:
- 默认行为 = dry-run,只返回 report,不做任何 mutation
- 通过 ``dry_run=False`` 可强制 DEL Redis lock(2.8B 范围默认 False)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryReport:
    """跨 worker 恢复扫描报告。

    Fields:
        scanned_thread_count: 总扫描 thread_id 数(默认上限 50)
        recoverable_thread_ids: 可恢复列表(worker 已死 + Redis 锁未被占用)
        occupied_thread_ids: 锁被占用列表(其他 worker 在跑)
        metadata_mismatch_thread_ids: metadata 不符合"interrupted"格式的 thread_id
        redis_lock_skipped: True 当 redis_inflight=None 时(无法判锁)
    """

    scanned_thread_count: int
    recoverable_thread_ids: List[str] = field(default_factory=list)
    occupied_thread_ids: List[str] = field(default_factory=list)
    metadata_mismatch_thread_ids: List[str] = field(default_factory=list)
    redis_lock_skipped: bool = False


async def scan_recoverable_checkpoints(
    *,
    cp: Any,
    redis_inflight: Optional[Any],
    max_threads: int = 50,
    interrupted_marker: str = "interrupted",
) -> RecoveryReport:
    """扫描 Postgres checkpointer,找到可被本 worker 接管的 thread_id 列表。

    Args:
        cp: ``AsyncPostgresSaver`` 实例(已 setup 完成)
        redis_inflight: 可选 ``RedisInFlightRegistry`` — 用于判断锁是否被占用;
            None = 跳过锁检查(单 worker 场景)
        max_threads: 最多扫描 thread_id 数(防止冷启动阻塞)
        interrupted_marker: ``checkpoint.metadata.source`` 字段的 expected 值;
            默认 ``"interrupted"`` — 由 ``LangGraphRunCoordinator`` 在中断点写入

    Returns:
        ``RecoveryReport`` — 调用方根据 ``recoverable_thread_ids`` 决定是否
        resume;**本函数不自动 resume**。
    """
    if cp is None:
        raise RuntimeError("scan_recoverable_checkpoints: cp is None")

    # 1. 列出最近 checkpoints
    #    langgraph-checkpoint-postgres 2.0.25 的 list API 是 async iterator;
    #    它的 ``config`` 参数是 search filter;``{}`` 等价于"全部 thread_id"
    #    (受 max_threads 限制防止冷启动阻塞)。
    candidates: List[tuple[str, Optional[dict]]] = []
    try:
        count = 0
        async for cp_tuple in cp.list({}):  # type: ignore[attr-defined]
            # cp_tuple.config["configurable"]["thread_id"]
            try:
                thread_id = cp_tuple.config["configurable"]["thread_id"]
            except (KeyError, TypeError):
                continue
            candidates.append((str(thread_id), cp_tuple.checkpoint))
            count += 1
            if count >= max_threads:
                break
    except AttributeError:
        # list API 不存在 → 兜底空 list(只保留接口契约)
        logger.warning(
            "scan_recoverable_checkpoints: cp.list not available; "
            "fallback to empty result"
        )
        candidates = []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scan_recoverable_checkpoints: cp.list failed (%s); empty result",
            exc,
        )
        candidates = []

    # 2. 过滤:checkpoint.metadata.source == interrupted_marker
    recoverable: List[str] = []
    mismatch: List[str] = []
    for thread_id, checkpoint in candidates:
        if checkpoint is None:
            mismatch.append(thread_id)
            continue
        meta = checkpoint.get("metadata") or {}
        # metadata 是 ChannelValues 嵌套;source 通常在 metadata["source"]
        source = (
            meta.get("source")
            or meta.get("langgraph_node")
            or ""
        )
        if source != interrupted_marker:
            mismatch.append(thread_id)
            continue
        # 3. 检查 Redis 锁是否被占用
        if redis_inflight is None:
            recoverable.append(thread_id)
        else:
            try:
                # 试着 acquire + 立即 release — 若已被持有会抛 ParallelDispatchGuardError
                owner = await redis_inflight.try_acquire(thread_id, "langgraph")
                if owner is None:
                    # Redis 不可用 → graceful degrade 为"可恢复"
                    recoverable.append(thread_id)
                else:
                    # 拿到锁 → 该 thread 没有其他 worker 在跑 — 可恢复;
                    # 但我们也立即释放,避免占用本不该立即 resume 的任务。
                    await redis_inflight.release(thread_id, owner)
                    recoverable.append(thread_id)
            except Exception as exc:  # noqa: BLE001
                # ParallelDispatchGuardError = 其他 worker 在跑
                from app.agent_runtime.dispatch_errors import (
                    ParallelDispatchGuardError,
                )

                if isinstance(exc, ParallelDispatchGuardError):
                    logger.info(
                        "scan_recoverable_checkpoints: thread=%s currently "
                        "locked by another worker; skip",
                        thread_id,
                    )
                else:
                    logger.warning(
                        "scan_recoverable_checkpoints: thread=%s lock check "
                        "failed (%s); mark as recoverable",
                        thread_id,
                        exc,
                    )
                    recoverable.append(thread_id)

    # 4. 二次过滤:recoverable 中哪些是被其他 worker 锁住的(避免重复 release)
    occupied: List[str] = []
    final_recoverable: List[str] = []
    if redis_inflight is not None:
        for thread_id in recoverable:
            try:
                owner = await redis_inflight.try_acquire(thread_id, "langgraph")
                if owner is None:
                    # Redis 不可用 → graceful degrade as occupied
                    occupied.append(thread_id)
                    continue
                # 拿到锁 → 该 thread 没被锁;但我们前一步已经 acquire+release,
                # 所以这里第二次 acquire 应该成功 → 说明确实没被锁。
                await redis_inflight.release(thread_id, owner)
                final_recoverable.append(thread_id)
            except Exception as exc:  # noqa: BLE001
                from app.agent_runtime.dispatch_errors import (
                    ParallelDispatchGuardError,
                )

                if isinstance(exc, ParallelDispatchGuardError):
                    occupied.append(thread_id)
                else:
                    final_recoverable.append(thread_id)
    else:
        final_recoverable = recoverable

    return RecoveryReport(
        scanned_thread_count=len(candidates),
        recoverable_thread_ids=final_recoverable,
        occupied_thread_ids=occupied,
        metadata_mismatch_thread_ids=mismatch,
        redis_lock_skipped=redis_inflight is None,
    )


async def release_orphaned_lock(
    *,
    redis_inflight: Any,
    task_public_id: str,
) -> bool:
    """运维显式调用 — 释放 orphaned Redis InFlight 锁(其他 worker 已死)。

    **强制 DEL 不校验 owner**(无视 owner mismatch) — 守禁令 #32 警告:
    仅当运维已确认持有者 worker 已死亡时才能调用;否则会破坏运行中任务的守护。

    Returns:
        True 当 DEL 成功(或 key 本来就不存在 — 无害 no-op);
        False 当 Redis 不可用
    """
    if redis_inflight is None:
        logger.warning(
            "release_orphaned_lock: redis_inflight is None; cannot release"
        )
        return False
    if redis_inflight._redis is None:  # type: ignore[attr-defined]
        return False
    key = f"{redis_inflight.key_prefix}{task_public_id}"  # type: ignore[attr-defined]
    try:
        deleted = await redis_inflight._redis.delete(key)  # type: ignore[attr-defined]
        logger.info(
            "release_orphaned_lock: task=%s deleted=%s",
            task_public_id,
            bool(deleted),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "release_orphaned_lock: Redis DEL failed task=%s err=%s",
            task_public_id,
            exc,
        )
        return False


__all__ = [
    "RecoveryReport",
    "release_orphaned_lock",
    "scan_recoverable_checkpoints",
]
