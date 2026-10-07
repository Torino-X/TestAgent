"""Phase 2.8B Postgres Checkpointer 实证 helpers — 验证 4 张核心表 + round-trip。

设计要点(对应 docs/30 §2 + ADR-2.8B-2/5/12):

* ``verify_postgres_4_tables(dsn) -> list[str]``:返回所有 4 张核心表;
  显式接收原始 DSN,内部规范化后用 psycopg 独立查 ``pg_tables``。
* ``roundtrip_checkpoint(cp, thread_id, payload) -> dict``:用
  ``AsyncPostgresSaver.put()`` 写一个 sentinel,然后 ``get()`` 读回。
* ``count_checkpoints(cp, thread_id) -> int``:用于跨 worker 恢复断言。

Phase 2.8R-D 重写:
  * 接口改为 ``verify_postgres_4_tables(dsn)`` — 不再从 cp 对象猜 DSN
  * 调用方传入 ``settings.agent_runtime_postgres_url``
  * 内部先调 ``normalize_postgres_conn_string`` 去掉 ``+psycopg`` suffix
  * 查询失败 → raise RuntimeError(fail-closed),不 assume verify OK
  * 日志脱敏,不泄露密码
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Optional

from .postgres_checkpointer import normalize_postgres_conn_string

logger = logging.getLogger(__name__)


# 4 张核心表 — 由 ``langgraph-checkpoint-postgres`` 2.0.25 ``setup()`` 创建
EXPECTED_CORE_TABLES = frozenset({
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
})


def _mask_dsn(dsn: str) -> str:
    """脱敏:``postgresql://user:secret@host:5432/db`` → ``postgresql://user:***@host:5432/db``。"""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)


async def verify_postgres_4_tables(dsn: str) -> List[str]:
    """验证 Postgres checkpointer 真实创建了 4 张核心表。

    直接用 psycopg 对规范化后的 DSN 建短连接查 ``pg_tables``。
    调用方应传入原始 DSN (可含 ``+psycopg`` suffix),本函数内部规范化。

    Args:
        dsn: 原始或规范化后的 libpq DSN

    Returns:
        4 张核心表的 sorted list

    Raises:
        RuntimeError 当 DSN 为空 / psycopg 不可用 / 查询失败 / 缺表
    """
    if not dsn or not dsn.strip():
        raise RuntimeError("verify_postgres_4_tables: DSN 为空")

    # 规范化:去掉 +psycopg / +asyncpg,ssl=false → sslmode=disable
    normalized = normalize_postgres_conn_string(dsn.strip())
    dsn_echo = _mask_dsn(normalized)

    present: set[str] = set()
    try:
        import psycopg  # type: ignore
        async with await psycopg.AsyncConnection.connect(
            normalized, autocommit=True,
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                )
                rows = await cur.fetchall()
                for row in rows:
                    present.add(str(row[0]))
    except ImportError as exc:
        raise RuntimeError(
            f"verify_postgres_4_tables: psycopg 不可用({exc}); "
            f"dsn={dsn_echo} "
            "无法独立验证 checkpoint 表,readiness=False"
        ) from exc
    except Exception as exc:
        # 脱敏:只输出 dsn_echo,不输出原始 dsn 或 exc 中的完整连接串
        raise RuntimeError(
            f"verify_postgres_4_tables: 查询失败({type(exc).__name__}: {str(exc)[:200]}); "
            f"dsn={dsn_echo} "
            "无法确认 checkpoint 表存在,readiness=False"
        ) from exc

    missing = EXPECTED_CORE_TABLES - present
    if missing:
        raise RuntimeError(
            f"Postgres checkpoint table verify failed: "
            f"missing={sorted(missing)} present={sorted(present)} "
            f"dsn={dsn_echo}"
        )

    logger.info(
        "Postgres checkpoint table verify success | tables=%d/%d | %s | dsn=%s",
        len(EXPECTED_CORE_TABLES),
        len(present & EXPECTED_CORE_TABLES),
        sorted(present & EXPECTED_CORE_TABLES),
        dsn_echo,
    )
    return sorted(EXPECTED_CORE_TABLES)


async def roundtrip_checkpoint(
    cp: Any,
    *,
    thread_id: str,
    payload: Any,
    checkpoint_ns: str = "",
) -> Optional[dict]:
    """把 ``payload`` 写入 checkpoint → 读回 → 返回读到的 checkpoint dict。

    Args:
        cp: ``AsyncPostgresSaver`` 实例
        thread_id: 测试用唯一 ID(避免污染其他 thread)
        payload: 任意 JSON-serializable dict;写入 ``channel_values``
        checkpoint_ns: 默认 ``""``(langgraph 默认命名空间)

    Returns:
        读回的 checkpoint 字典(``config``, ``checkpoint``, ``metadata``, ``parent_config``)
        当无检查点存在时返回 ``None``
    """
    if cp is None:
        raise RuntimeError("roundtrip_checkpoint: cp is None")

    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
    }
    saved = await cp.put(
        config,
        checkpoint={"v": 1, "marker": payload},
        metadata={"source": "phase_2.8b_integration_test"},
        new_versions={"v": 1},
    )
    logger.info(
        "roundtrip_checkpoint: wrote thread=%s saved_id=%s",
        thread_id,
        saved.get("configurable", {}).get("checkpoint_id"),
    )

    latest = await cp.get(config)
    if latest is None:
        logger.warning("roundtrip_checkpoint: get() returned None for thread=%s", thread_id)
        return None

    logger.info(
        "roundtrip_checkpoint: read thread=%s checkpoint_id=%s",
        thread_id,
        latest.get("config", {}).get("configurable", {}).get("checkpoint_id"),
    )
    return {
        "config": latest.get("config"),
        "checkpoint": latest.get("checkpoint"),
        "metadata": latest.get("metadata"),
        "parent_config": latest.get("parent_config"),
    }


async def count_checkpoints(cp: Any, *, thread_id: str) -> int:
    """返回 ``thread_id`` 下存在的 checkpoint 数。"""
    if cp is None:
        raise RuntimeError("count_checkpoints: cp is None")

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    count = 0
    try:
        async for _ in cp.list(config):  # type: ignore[attr-defined]
            count += 1
    except AttributeError:
        latest = await cp.get(config)
        return 1 if latest is not None else 0
    return count


__all__ = [
    "EXPECTED_CORE_TABLES",
    "count_checkpoints",
    "roundtrip_checkpoint",
    "verify_postgres_4_tables",
]
