"""Confirmation timeout CLI —— Phase 2.2 管理命令。

**不挂 API**(规范 §17.3);仅供 ops 手动触发或外部 scheduler 周期调:

    python -m app.agent_runtime.confirmation_timeout_cli             # 扫一次
    python -m app.agent_runtime.confirmation_timeout_cli --timeout-seconds 600

设计:
* 复用项目里的 AsyncSession 生成器(``app.db.session.get_async_session``)。
* 不接 LangGraph coordinator(因为 CLI 进程通常独立,只负责 CAS 标记;真正的
  resume 由调用方在拿到 ``timed_out`` 计数后调度)。
* Phase 2.2 范围:仅扫 + CAS,不直接调 coordinator resume;Phase 2.3+ 接
  APScheduler / Celery beat 时再加完整链路。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from app.db.session import get_async_session
from .confirmation_timeout_service import (
    DEFAULT_TIMEOUT_SECONDS,
    ConfirmationTimeoutService,
)

logger = logging.getLogger(__name__)


async def _run(*, timeout_seconds: int) -> int:
    service = ConfirmationTimeoutService(
        session_factory=get_async_session,
        clock=lambda: __import__("datetime").datetime.utcnow(),
        timeout_seconds=timeout_seconds,
    )
    result = await service.scan_and_drive_once()
    print(
        f"confirmation-timeout: scanned={result.scanned}, "
        f"timed_out={result.timed_out}, driven={result.driven}, "
        f"failed={result.failed}, skipped_locked={result.skipped_locked}"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scan timed-out human confirmations")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Confirmations older than this are marked timeout (default: 300)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(timeout_seconds=args.timeout_seconds))


if __name__ == "__main__":
    sys.exit(main())

# 模块定位:Confirmation Timeout 管理命令 CLI
#
# 不暴露 HTTP API(规范 §17.3),仅给 ops 或外部 cron 调用:
#   python -m app.agent_runtime.confirmation_timeout_cli
#   python -m app.agent_runtime.confirmation_timeout_cli --timeout-seconds 600
#
# 链路:
#   调用 ConfirmationTimeoutService → 扫描超时的 HumanConfirmation 行 →
#   写 audit + emit TASK_RESUMED(with source='timeout')→ LangGraph 重新拉起
#   section_confirmation_interrupt(由 LangGraphRunCoordinator.resume_*)。
#
# 关键约束:
#   - 必须 idempotent:同一 task 多次扫描不会重复 emit;
#   - 超时时间按 user config(默认 300s);
#   - 失败仅 LOG,不 raise(管理命令不应崩溃调度器)。
