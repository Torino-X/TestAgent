"""Deadline / 取消工具：Deadline + CancellationToken 适配。

CE-02 WP-2：预算耗尽（deadline exhausted）与 cancel / timeout / degraded /
required_failure 各自独立分类。
"""

from __future__ import annotations

from dataclasses import dataclass


class DeadlineExceeded(Exception):
    """预算耗尽（deadline exhausted），非取消。"""


@dataclass(frozen=True)
class Deadline:
    """剩余预算检查。``deadline_ms`` 为 0/None 表示无预算限制。"""

    deadline_ms: int | None = None
    start_epoch_ms: int = 0

    def remaining_ms(self, now_epoch_ms: int) -> int:
        if self.deadline_ms is None:
            return -1
        return max(0, self.deadline_ms - (now_epoch_ms - self.start_epoch_ms))

    def is_expired(self, now_epoch_ms: int) -> bool:
        if self.deadline_ms is None:
            return False
        return (now_epoch_ms - self.start_epoch_ms) >= self.deadline_ms

    def raise_if_expired(self, now_epoch_ms: int) -> None:
        if self.is_expired(now_epoch_ms):
            raise DeadlineExceeded("context source deadline exhausted")


class CancellationToken:
    """读取 ``runtime_context.cancellation_service`` 的取消检查。

    取消不得转为 degraded warning（错误分类独立传播）。
    """

    def __init__(self, cancellation_service, task_id: str | None = None) -> None:
        self._cancellation_service = cancellation_service
        self._task_id = task_id

    @property
    def task_id(self) -> str | None:
        return self._task_id

    def is_cancelled(self) -> bool:
        if self._cancellation_service is None:
            return False
        if self._task_id is None:
            return False
        return bool(self._cancellation_service.is_cancelled(self._task_id))
# auto-appended module-level note: deadline 计时 source。
