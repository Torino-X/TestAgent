"""Database session management.

Provides both a synchronous session factory (used by Alembic and scripts)
and an async session factory (used by FastAPI dependency injection).

URL selection:
  - sync_engine   uses settings.sync_database_url   (mysql+pymysql://)
  - async_engine  uses settings.async_database_url   (mysql+aiomysql://)

Logging never prints the full database URL.

Phase 2.8R-K: connect_args 强制设置 MySQL session sql_mode。
本地开发机 MySQL 8 默认 NO_ZERO_DATE,与老 migration 兼容性差;
去掉 NO_ZERO_DATE(保留 STRICT_TRANS_TABLES)能让 DDL 通过。
生产在云上后建议 STRICT_ALL_TABLES + NO_ZERO_DATE。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


# ── MySQL session sql_mode — Phase 2.8R-K 严格化 ──────────────────


def _strip_no_zero_date_sql_mode(current: str) -> str:
    """从 sql_mode 串里去掉 NO_ZERO_DATE / NO_ZERO_IN_DATE。

    MySQL 8 默认含:
      ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,
      ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION

    老 migration 用 0000-00-00 默认值(NO_ZERO_DATE 不允许),
    开发环境去掉 NO_ZERO_DATE / NO_ZERO_IN_DATE 让 migration 通过。
    """
    if not current:
        return "STRICT_TRANS_TABLES"
    parts = [p.strip() for p in current.split(",") if p.strip()]
    parts = [
        p for p in parts
        if p not in ("NO_ZERO_DATE", "NO_ZERO_IN_DATE")
    ]
    return ",".join(parts) if parts else "STRICT_TRANS_TABLES"


# ── Sync engine (Alembic, scripts, one-off queries) ─────────────────────

sync_engine = create_engine(
    settings.sync_database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=5,
    max_overflow=10,
    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
    json_deserializer=json.loads,
)

SyncSessionLocal = sessionmaker(
    sync_engine,
    class_=Session,
    expire_on_commit=False,
)

# ── Async engine (FastAPI endpoints) ────────────────────────────────────

async_engine = create_async_engine(
    settings.async_database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
    json_deserializer=json.loads,
)

AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ── Session-level sql_mode override ─────────────────────────────────────
# 每次新建 connection,设置 sql_mode 去掉 NO_ZERO_DATE/NO_ZERO_IN_DATE。
# 用 SQLAlchemy event hooks(必须在 sync_engine / async_engine 定义后才能注册)。
from sqlalchemy import event  # noqa: E402


@event.listens_for(sync_engine, "connect")
def _set_sync_sql_mode(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SELECT @@SESSION.sql_mode")
        current = cursor.fetchone()[0] or ""
        new_mode = _strip_no_zero_date_sql_mode(current)
        cursor.execute(f"SET SESSION sql_mode = '{new_mode}'")
    finally:
        cursor.close()


@event.listens_for(async_engine.sync_engine, "connect")
def _set_async_sql_mode(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SELECT @@SESSION.sql_mode")
        current = cursor.fetchone()[0] or ""
        new_mode = _strip_no_zero_date_sql_mode(current)
        cursor.execute(f"SET SESSION sql_mode = '{new_mode}'")
    finally:
        cursor.close()


# ── FastAPI dependency ───────────────────────────────────────────────────

_DB_RETRY_EXC = (OperationalError, DisconnectionError, InterfaceError)


async def _open_validated_session() -> AsyncSession:
    """创建一个**已 warmed-up**的 AsyncSession。

    在 yield 给 caller 前强制发一次 ``SELECT 1`` 探测底层连接,把
    ``pool.connect()`` 阶段的瞬时 OperationalError 暴露出来;这样
    tenacity 才能在 caller 拿到 session 前重试,而不是把死连接交给
    业务代码。
    """
    session = AsyncSessionLocal()
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        await session.close()
        raise
    return session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async DB session per request.

    偶发 MySQL 抖动(连接池拿到死连接 / 握手后被服务端 RST)由
    tenacity 自动重试 3 次,指数退避 0.1s→2s,业务异常不重试。
    """
    session: AsyncSession | None = None
    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_DB_RETRY_EXC),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=2.0),
            reraise=True,
        ):
            with attempt:
                if session is not None:
                    await session.close()
                session = await _open_validated_session()
        yield session
        await session.commit()
    except Exception:
        if session is not None:
            await session.rollback()
        raise
    finally:
        if session is not None:
            await session.close()
