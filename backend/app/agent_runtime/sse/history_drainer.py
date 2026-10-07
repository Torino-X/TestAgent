"""HistoryDrainer — Phase 2.6 SSE Last-Event-ID 实时 + 历史回放。

调用模式(SSE 入口):
  1. 解析请求头 ``Last-Event-ID``(public_id 或 sequence_no)
  2. ``SELECT ... FROM agent_events WHERE task_id = :tid AND sequence_no > :last_seq`` —— 历史 replay
  3. ``subscribe(task_id, queue=local_queue)`` —— LiveEventBus
  4. yield 历史 + 实时 直到 stream_done

设计要点(ADR-2.6-6):
  * HistoryDrainer **不替换** 现有 ``agent_tasks.py:71-267 event_stream()``
    (URL/响应头/事件名零变化);它作为可选 opt-in 组件,Phase 2.6 默认不动
    生产热路径;Phase 2.7 才逐步落地。
  * 通过 ``drain(...)`` 异步 generator 接口输出 dict list(caller 决定怎么渲染为 SSE 帧)。
  * Last-Event-ID 解析:
      - 整数 → sequence_no 比较
      - UUID7 / 字符串 → public_id 比较

约束:
  * 不抛;replay 失败 → log + 走空路径
  * 退出时 ``unsubscribe`` 防止 queue 泄漏
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_LAST_EVENT_ID_INT = re.compile(r"^\d{1,20}$")


def parse_last_event_id(value: str | None) -> dict[str, Any]:
    """解析 Last-Event-ID 头。

    返回 ``{"sequence_no": int | None, "public_id": str | None}``。
    """
    if not value:
        return {"sequence_no": None, "public_id": None}
    v = value.strip()
    if not v:
        return {"sequence_no": None, "public_id": None}
    if _LAST_EVENT_ID_INT.match(v):
        return {"sequence_no": int(v), "public_id": None}
    return {"sequence_no": None, "public_id": v}


class HistoryDrainer:
    """按 ``(task_id, last_event_id)`` 拉历史 → 订阅 LiveEventBus 实时 → yield 合并流。

    构造参数:
      * ``session_factory`` — ``Callable[[], AsyncContextManager[AsyncSession]]``
      * ``bus`` — 任意 ``LiveEventBusProtocol``(InMemory 或 Redis)
      * ``task_id`` — 任务 public_id 或 internal_id 都行;内部统一转 int
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        bus: Any,
        task_id: int | str,
        task_internal_id: int,
        replay_limit: int = 500,
        live_poll_interval: float = 0.5,
        live_timeout_seconds: float = 60.0 * 30.0,
    ) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._task_id = str(task_id)
        self._task_internal_id = int(task_internal_id)
        self._replay_limit = int(replay_limit)
        self._live_poll_interval = live_poll_interval
        self._live_timeout_seconds = float(live_timeout_seconds)

    async def drain(
        self,
        *,
        last_event_id: Optional[str] = None,
        stop_when: Optional[Callable[[dict], bool]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """异步生成器。

        顺序:
          1. replay(写 DB 阶段的事件)
          2. emit ``{"event_type": "task_replay_complete"}`` 控制帧(Phase 2.6 新事件)
          3. subscribe live;yield live events
          4. stop_when(event) True → break

        Args:
          * ``last_event_id``: SSE Last-Event-ID header 内容
          * ``stop_when``: caller 控制 (返回 True 即停止 drain)

        Yields:
          dict — history row 字典(event_type / title / content / payload /
          sequence_no / event_id / node_name / graph_run_id / idempotency_key)
        """
        parsed = parse_last_event_id(last_event_id)
        replayed: set[str] = set()

        # 1) 历史回放
        try:
            async for row in self._replay(parsed):
                replayed.add(str(row.get("public_id") or ""))
                yield row
        except Exception:
            logger.warning(
                "HistoryDrainer: replay failed (continuing with empty history)",
                exc_info=True,
            )

        # 2) replay 边界控制帧(让前端知道从哪里开始是 live)
        yield {
            "event_type": "task_replay_complete",
            "task_id": self._task_id,
            "payload": {"replayed_count": len(replayed)},
            "created_at": _iso_now(),
        }

        # 3) live
        if not hasattr(self._bus, "subscribe"):
            return

        local_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        try:
            await self._bus.subscribe(task_id=self._task_id, queue=local_queue)
        except Exception:
            logger.warning(
                "HistoryDrainer: bus subscribe failed; draining ends here",
                exc_info=True,
            )
            return

        try:
            t0 = asyncio.get_event_loop().time()
            while True:
                now = asyncio.get_event_loop().time()
                if now - t0 > self._live_timeout_seconds:
                    break
                try:
                    event = await asyncio.wait_for(
                        local_queue.get(), timeout=self._live_poll_interval
                    )
                except asyncio.TimeoutError:
                    continue

                if not isinstance(event, dict):
                    continue
                # 过滤已知 repeat(已在 replay 中出现过 + live 又来一次 — 去重)
                ev_id = event.get("event_id") or event.get("public_id")
                if ev_id and str(ev_id) in replayed:
                    continue

                yield event
                if stop_when is not None and stop_when(event):
                    break
        finally:
            # 干净退出 — unsubscribe 慢消费者等不会泄漏
            try:
                await self._bus.unsubscribe(
                    task_id=self._task_id, queue=local_queue
                )
            except Exception:
                pass

    async def _replay(
        self, parsed: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        if parsed["sequence_no"] is not None:
            cond = "AND sequence_no > :last_seq"
            params: dict[str, Any] = {
                "tid": self._task_internal_id,
                "last_seq": int(parsed["sequence_no"]),
                "limit": self._replay_limit,
            }
        elif parsed["public_id"]:
            cond = "AND public_id > :last_pid"
            params = {
                "tid": self._task_internal_id,
                "last_pid": parsed["public_id"],
                "limit": self._replay_limit,
            }
        else:
            cond = ""
            params = {
                "tid": self._task_internal_id,
                "limit": self._replay_limit,
            }

        async with self._session_factory() as session:
            assert isinstance(session, AsyncSession)
            result = await session.execute(
                text(
                    f"SELECT public_id, event_type, message_type, title, content, "
                    f"       payload_json, status, sequence_no, graph_run_id, "
                    f"       graph_version, node_name, event_schema_version, "
                    f"       idempotency_key, created_at "
                    f"FROM agent_events WHERE task_id = :tid {cond} "
                    f"ORDER BY COALESCE(sequence_no, 0) ASC, id ASC LIMIT :limit"
                ),
                params,
            )
            rows = result.fetchall()
            for r in rows:
                yield {
                    "public_id": r[0],
                    "event_id": r[0],
                    "event_type": r[1],
                    "message_type": r[2],
                    "title": r[3],
                    "content": r[4],
                    "payload": _parse_payload_json(r[5]),
                    "status": r[6],
                    "sequence_no": r[7],
                    "graph_run_id": r[8],
                    "graph_version": r[9],
                    "node_name": r[10],
                    "event_schema_version": r[11],
                    "idempotency_key": r[12],
                    "created_at": r[13],
                    "task_id": self._task_id,
                }


def _parse_payload_json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return _json.loads(value)
        except (ValueError, TypeError):
            return {}
    return {}


def _iso_now() -> str:
    return datetime.utcnow().isoformat()


__all__ = ["HistoryDrainer", "parse_last_event_id"]
