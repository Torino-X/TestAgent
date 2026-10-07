"""ConfirmationTimeoutService —— Phase 2.2 超时驱动。

**核心约束**(规范 §17.3):
* 超时**不**由 Worker 内存 ``asyncio.sleep`` 驱动;由 management command
  ``confirmation_timeout_cli.py`` 或外部 scheduler 周期触发。
* service 只暴露**纯函数 + 显式依赖**,避免和 asyncio 循环耦合。
* CAS 更新 ``human_confirmations.status``,避免与用户决策竞争。
* 使用 coordinator 的 ``resume_section_confirmation`` /
  ``resume_format_loss_interrupt`` 触发 ``Command(resume=..., source=timeout)``;
历史非 LangGraph 任务不会进入本服务的恢复路径。

**决策策略**(规范 §17.3.2):
* 章节确认 timeout → 自动 accept(走默认 section 集合,避免任务卡死)。
* 格式损失 timeout → 自动 accept(用户既未拒绝也无 retry 意图,保守接受)。
* 不调用 reject(reject 等同 fail_task,不适合 timeout 场景)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.human_confirmation import HumanConfirmation

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECONDS = 300


class ResumeCallback(Protocol):
    """外部 coordinator 接口。coordinator 注入而非 service 直接依赖。"""

    async def __call__(
        self, *, task_public_id: str, task_id: str,
        kind: str, decision: Dict[str, Any],
    ) -> bool:
        """返回 True=成功驱动 / False=驱动失败(被 service 忽略)."""


@dataclass(frozen=True)
class TimeoutScanResult:
    """一次扫描的统计 + 决策。"""

    scanned: int
    timed_out: int
    driven: int
    failed: int
    skipped_locked: int


class ConfirmationTimeoutService:
    """扫描超时 human_confirmations → CAS 更新 → 调 resume callback。

    :param session_factory: 产生新 AsyncSession 的 callable;service 短事务
                           边界由 caller 控制。
    :param resume_callback: 注入 coordinator 行为(避免 service 直接依赖
                            coordinator 模块)。
    :param clock: 可注入时钟,便于测试。
    :param timeout_seconds: 默认 300,可在构造时覆盖。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Awaitable[AsyncSession]],
        resume_callback: Optional[ResumeCallback] = None,
        clock: Callable[[], datetime] = lambda: datetime.utcnow(),
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._resume_callback = resume_callback
        self._clock = clock
        self._timeout_seconds = int(timeout_seconds)

    @property
    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    async def scan_and_drive_once(self) -> TimeoutScanResult:
        """扫描一次超时记录并尝试驱动 coordinator resume。

        流程:
        1. 取所有 ``status=pending`` 且 ``requested_at < now - timeout_seconds`` 的行。
        2. 对每行构造 CAS UPDATE:``WHERE id=:id AND status='pending'``。
           成功 = 当前是 timeout service 抢到 → 调 callback;rowcount=0 = 用户
           已决策或被另一个 worker 抢先。
        3. 累加 counts,返回统计。
        """
        now = self._clock()
        threshold = now - timedelta(seconds=self._timeout_seconds)
        session = await self._session_factory()
        try:
            candidates = await self._list_pending(session, threshold=threshold)
            scanned = len(candidates)
            timed_out = 0
            driven = 0
            failed = 0
            skipped_locked = 0
            for record in candidates:
                decision = _decision_for_type(record.confirmation_type, record)
                if decision is None:
                    skipped_locked += 1
                    continue
                cas_ok = await self._cas_mark_timed_out(
                    session, confirmation_id=record.id, now=now,
                    response=decision,
                )
                if not cas_ok:
                    skipped_locked += 1
                    continue
                timed_out += 1
                if self._resume_callback is None:
                    continue
                try:
                    ok = await self._resume_callback(
                        task_public_id=record.public_id,
                        task_id=str(record.task_id),
                        kind=record.confirmation_type,
                        decision=decision,
                    )
                    if ok:
                        driven += 1
                    else:
                        failed += 1
                except Exception:
                    logger.warning(
                        "ConfirmationTimeoutService: resume callback failed for "
                        "task_public_id=%s kind=%s (swallowed)",
                        record.public_id,
                        record.confirmation_type,
                        exc_info=True,
                    )
                    failed += 1
            await session.commit()
            return TimeoutScanResult(
                scanned=scanned,
                timed_out=timed_out,
                driven=driven,
                failed=failed,
                skipped_locked=skipped_locked,
            )
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def _list_pending(
        self, session: AsyncSession, *, threshold: datetime
    ) -> List[HumanConfirmation]:
        result = await session.execute(
            select(HumanConfirmation).where(
                HumanConfirmation.status == "pending",
                HumanConfirmation.requested_at < threshold,
            )
        )
        return list(result.scalars().all())

    async def _cas_mark_timed_out(
        self,
        session: AsyncSession,
        *,
        confirmation_id: int,
        now: datetime,
        response: Dict[str, Any],
    ) -> bool:
        import json as _json

        resp_str = _json.dumps(response, ensure_ascii=False, default=str)
        result = await session.execute(
            update(HumanConfirmation)
            .where(
                HumanConfirmation.id == confirmation_id,
                HumanConfirmation.status == "pending",
            )
            .values(
                status="timeout",
                response_json=resp_str,
                expired_at=now,
                confirmed_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return bool(getattr(result, "rowcount", 0))


def _decision_for_type(
    confirmation_type: str, record: HumanConfirmation
) -> Optional[Dict[str, Any]]:
    """根据 confirmation_type 合成 timeout 决策。

    Phase 2.2:
    * section_confirmation → accept(把 request_json.sections 当作默认接受集合)。
    * format_loss_decision → accept。
    * 其它类型 → None(本 service 跳过,留给后续 Phase 接入)。
    """
    if confirmation_type == "section_confirmation":
        request = record.request_json or {}
        sections = request.get("sections") if isinstance(request, dict) else None
        return {
            "kind": "section_confirmation",
            "sections": sections if isinstance(sections, list) else [],
            "source": "timeout",
        }
    if confirmation_type == "format_loss_decision":
        return {
            "kind": "format_loss",
            "decision": "accept",
            "source": "timeout",
        }
    return None


__all__ = [
    "ConfirmationTimeoutService",
    "TimeoutScanResult",
    "ResumeCallback",
    "DEFAULT_TIMEOUT_SECONDS",
]

# 模块定位:ConfirmationTimeoutService(Phase 2.2 纯函数超时驱动)
#
# **核心约束**(规范 §17.3):
#   - 超时**不**由 Worker 内存 asyncio.sleep 驱动;
#   - 由 management command confirmation_timeout_cli.py 或外部 scheduler
#     周期触发;
#   - 本 service 只暴露**纯函数 + 显式依赖**,避免与 asyncio 循环耦合。
#
# 链路:
#   trigger → service.scan_pending(timeouts) → 写 audit + emit TASK_RESUMED →
#   LangGraph 重新拉起 section_confirmation_interrupt / format_loss_interrupt
#
# 关键约束:
#   - 必须 idempotent:同一 task 扫描多次不会重复 emit;
#   - 超时阈值按 user config(默认 300s);
#   - 失败仅 LOG,不 raise(管理命令不应崩溃调度器)。
