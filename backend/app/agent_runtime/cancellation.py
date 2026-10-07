"""进程内 ``CancellationService`` stub - Phase 2.0 范围。

**禁止** 在本阶段把它接入真实 SSE 取消信号 (规范 ADR-009:Phase 2.1 接入)。
"""

from __future__ import annotations

import threading
from typing import Dict


class InMemoryCancellationService:
    """线程安全的内存取消表。Phase 2.0 测试可显式注入。"""

    def __init__(self) -> None:
        self._cancelled: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def cancel(self, task_id: str) -> None:
        with self._lock:
            self._cancelled[task_id] = True

    def clear(self, task_id: str) -> None:
        with self._lock:
            self._cancelled.pop(task_id, None)

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return self._cancelled.get(task_id, False)


__all__ = ["InMemoryCancellationService"]

# 模块定位:进程内 CancellationService stub(Phase 2.0 占位)
#
# ⚠️ **Phase 2.1 起不应接入真实 SSE 取消信号**(规范 ADR-009)
# Phase 2.6 之后改用 cancellation_distributed.CancellationService(DB + Redis)
# 本模块仍是单进程 in-memory 占位,用于未启用 distributed 模式的测试。
#
# 关键约束:
#   - 不阻塞主 asyncio 循环;
#   - 不持久化(进程退出 → 取消信号丢失);
#   - 提供 is_cancelled(task_id) 同步接口,供子图循环迭代时探测。
