"""Postgres Checkpointer 工厂与健康探测 — Phase 2.8A。

设计要点:
* 只暴露 ``probe_postgres_checkpointer`` + ``build_postgres_checkpointer`` 两个
  异步入口;不在模块级直接 import langgraph.checkpoint.postgres (避免未装
  asyncpg 的环境启动失败)。
* ``probe_postgres_checkpointer(url) -> bool``:lifespan 启动期探测;失败
  立即返回 False,绝不抛错。
* ``build_postgres_checkpointer(url, *, setup=True) -> AsyncPostgresSaver | None``:
  连接 + 可选建表,失败返回 None(callers 必须接受 None 兜底 → MemorySaver)。
* Windows 兼容:``asyncio.run()`` 默认 ``ProactorEventLoop`` 下 psycopg 报错;
  在调用 ``from_conn_string`` 之前切换 ``SelectorEventLoop`` 即可。
  Linux/CI 默认 ``SelectorEventLoop``,无副作用。
* 不引入 Alembic 迁移;``setup()`` 自动建 6 张检查点表(checkpoints /
  checkpoint_blobs / checkpoint_writes / checkpoint_migrations + 相关索引)。

守禁令:
* 不在 GraphState 放 Session/LLMClient 等;本模块只返回 checkpointer 实例。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


def normalize_postgres_conn_string(url: str) -> str:
    """Convert legacy SQLAlchemy Postgres URLs to psycopg-compatible URLs.

    ``AsyncPostgresSaver`` uses psycopg directly.  It accepts libpq-style
    ``postgresql://`` URLs, while existing application settings may still use
    SQLAlchemy's ``postgresql+asyncpg://`` scheme and ``ssl=false`` option.
    """
    # 剥掉意外的外层引号和 CR/LF
    url = str(url or "").strip().strip("'\"\r\n")
    if not url:
        return ""
    parsed = urlsplit(url)
    # SQLAlchemy accepts driver-qualified schemes (for example
    # ``postgresql+asyncpg://`` and ``postgresql+psycopg://``), while psycopg
    # expects the libpq form without the ``+driver`` suffix.
    scheme = "postgresql" if parsed.scheme.startswith("postgresql+") else parsed.scheme
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    has_sslmode = any(key.lower() == "sslmode" for key, _ in query_items)

    if not has_sslmode:
        normalized_items = []
        for key, value in query_items:
            if key.lower() == "ssl" and value.lower() in {"0", "false", "no", "off"}:
                normalized_items.append(("sslmode", "disable"))
            elif key.lower() == "ssl" and value.lower() in {"1", "true", "yes", "on"}:
                normalized_items.append(("sslmode", "require"))
            else:
                normalized_items.append((key, value))
        query_items = normalized_items

    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, urlencode(query_items), parsed.fragment)
    )


def _ensure_selector_event_loop() -> None:
    """Windows 兼容:检查当前 event loop policy,如果是 Proactor 则提示警告。

    Phase 2.8R-D:在 lifespan 中也主动尝试把 policy 切到 Selector(限一次;
    后续运行中切换无副作用)。生产推荐走 ``scripts/run_dev.py`` 显式开启
    Selector。在 ``uvicorn app.main:app --reload`` 直起场景,这里在 lifespan
    内做兜底。
    """
    if sys.platform == "win32":
        try:
            current = asyncio.get_event_loop_policy().__class__.__name__
            if current != "WindowsSelectorEventLoopPolicy":
                asyncio.set_event_loop_policy(
                    asyncio.WindowsSelectorEventLoopPolicy()
                )
                logger.info(
                    "Postgres Checkpointer: 已切换到 WindowsSelectorEventLoopPolicy "
                    "(lifespan 兜底;建议改用 scripts/run_dev.py)"
                )
        except RuntimeError:
            # 已有 loop 在运行;policy 切换已无效,只 WARN
            loop = asyncio.get_event_loop_policy().get_event_loop()
            if loop.__class__.__name__ == "ProactorEventLoop":
                logger.warning(
                    "Postgres Checkpointer: Windows ProactorEventLoop detected — "
                    "psycopg async requires SelectorEventLoop. "
                    "Call asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)."
                )
        # Always log the loop that will actually run psycopg. When the policy
        # switch above is a no-op (loop already running), this is the only
        # place that tells the operator whether psycopg will work.
        try:
            running_loop = asyncio.get_event_loop_policy().get_event_loop()
            logger.info(
                "Postgres Checkpointer: active event loop = %s",
                running_loop.__class__.__name__,
            )
        except RuntimeError:
            pass
    else:
        # Linux/CI:SelectorEventLoop 是默认,只 warnings 友好提示
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
            if loop.__class__.__name__ == "ProactorEventLoop":
                logger.warning(
                    "Postgres Checkpointer: Windows ProactorEventLoop detected — "
                    "psycopg async requires SelectorEventLoop. "
                    "Call asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)."
                )
        except RuntimeError:
            pass


def _running_loop_is_psycopg_compatible() -> bool:
    """Report the actual event loop used by the current async task.

    Changing a policy during FastAPI lifespan cannot replace Uvicorn's already
    running loop.  Psycopg checks ``get_running_loop()``, so diagnostics must
    inspect that same loop instead of a policy-created future loop.
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "Postgres Checkpointer: no running event loop is available for the "
            "async Psycopg probe"
        )
        return False

    loop_name = type(running_loop).__name__
    logger.info("Postgres Checkpointer: running event loop = %s", loop_name)

    if sys.platform == "win32" and isinstance(
        running_loop, asyncio.ProactorEventLoop
    ):
        logger.error(
            "Postgres Checkpointer: Windows ProactorEventLoop is incompatible "
            "with Psycopg async. Start the backend with `python scripts/run_dev.py "
            "--host 127.0.0.1 --port 8003`; changing the policy during lifespan "
            "is too late."
        )
        return False

    return True


async def probe_postgres_checkpointer(url: str, *, timeout: float = 5.0) -> bool:
    """Probe Postgres Checkpointer connectivity at lifespan startup.

    Returns True if ``setup()`` succeeds within ``timeout`` seconds.
    Never raises — failures log and return False.

    Args:
        url: ``postgresql://user:pass@host:port/dbname``. Legacy
            ``postgresql+asyncpg://`` URLs are normalized for compatibility.
        timeout: max seconds for connect + setup; default 5.0
    """
    if not url or not url.strip():
        logger.warning(
            "FALLBACK_USED | component=postgres_checkpointer | "
            "from=postgres_probe | to=memory_saver | reason=empty_url"
        )
        return False

    if not _running_loop_is_psycopg_compatible():
        logger.warning(
            "FALLBACK_USED | component=postgres_checkpointer | "
            "from=postgres_probe | to=memory_saver | "
            "reason=incompatible_running_event_loop"
        )
        return False
    conn_string = normalize_postgres_conn_string(url)

    async def _inner() -> bool:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        try:
            async with AsyncPostgresSaver.from_conn_string(conn_string) as cp:
                await asyncio.wait_for(cp.setup(), timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_USED | component=postgres_checkpointer | "
                "from=postgres_probe | to=memory_saver | reason=probe_failed | "
                "err_type=%s | err=%s",
                type(exc).__name__,
                str(exc)[:300],
            )
            return False

    try:
        return await asyncio.wait_for(_inner(), timeout=timeout + 1.0)
    except asyncio.TimeoutError:
        logger.warning(
            "FALLBACK_USED | component=postgres_checkpointer | "
            "from=postgres_probe | to=memory_saver | reason=probe_timeout | "
            "timeout=%s",
            timeout + 1.0,
        )
        return False


async def build_postgres_checkpointer(
    url: str,
    *,
    setup: bool = True,
    timeout: float = 5.0,
) -> Optional["object"]:
    """Connect to Postgres and return an ``AsyncPostgresSaver`` instance.

    The returned saver owns its connection pool — caller MUST close it on
    shutdown (``await cp.aclose()`` or use as ``async with``).

    Returns None on any failure (connect / setup / timeout); callers MUST
    tolerate None and fall back to ``build_inmemory_checkpointer()``.

    Args:
        url: same as ``probe_postgres_checkpointer``
        setup: if True, call ``cp.setup()`` to auto-create tables; default True
        timeout: max seconds for connect + setup; default 5.0
    """
    if not url or not url.strip():
        logger.warning(
            "FALLBACK_USED | component=postgres_checkpointer | "
            "from=postgres_build | to=memory_saver | reason=empty_url"
        )
        return None

    _ensure_selector_event_loop()
    conn_string = normalize_postgres_conn_string(url)

    async def _inner():
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # FIX-B: 使用 AsyncConnectionPool 而非单连接。
        # 单连接 AsyncPostgresSaver.from_conn_string 持有单一 AsyncConnection；
        # 当 PostgreSQL 服务器关闭该连接(idle_session_timeout / 网络中断)后，
        # saver 仍引用 broken connection → 后续 get_tuple/put 全部
        # OperationalError("the connection is closed")，且 retry 也复用坏连接。
        #
        # pool 方案：每次 checkpoint 操作经 get_connection(pool) 从 pool 获取
        # 健康连接。check 回调在 checkout 时验证连接存活（SELECT 1），
        # 坏连接被 pool 丢弃并自动重建 → 下一次操作获得新健康连接。
        #
        # max_lifetime=1800：连接最长存活 30min 强制轮换，避免长连接被服务器
        # idle 超时关闭后残留。
        async def _pg_health_check(conn) -> None:
            """pool check 回调：验证连接仍可执行查询，坏连接抛错让 pool 重建。"""
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    await cur.fetchone()
            except Exception:  # noqa: BLE001 — 任何异常都让 pool 丢弃此连接
                raise

        pool = AsyncConnectionPool(
            conn_string,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            min_size=1,
            max_size=10,
            max_idle=300.0,
            max_lifetime=1800.0,
            timeout=timeout,
            reconnect_timeout=timeout,
            check=_pg_health_check,
        )
        # ``open()`` defaults to ``wait=False``.  Calling ``cp.setup()``
        # immediately after that races the pool's first background connection
        # on remote Postgres and can be cancelled mid-query by the outer
        # timeout.  Wait for the minimum pool connection before handing the
        # pool to AsyncPostgresSaver.
        await pool.open(wait=True, timeout=timeout)
        cp = AsyncPostgresSaver(conn=pool)
        if setup:
            try:
                await asyncio.wait_for(cp.setup(), timeout=timeout)
            except Exception as exc:
                logger.warning(
                    "FALLBACK_USED | component=postgres_checkpointer | "
                    "from=postgres_build | to=memory_saver | reason=setup_failed | "
                    "err_type=%s | err=%s",
                    type(exc).__name__,
                    str(exc)[:300],
                )
                await pool.close()
                return None
        # 绑定 pool 到 saver,让 caller ``await saver.aclose()`` 关闭 pool。
        setattr(cp, "_pg_pool", pool)
        return cp

    try:
        # Pool establishment and the checkpointer schema setup are separate
        # bounded phases.  Each receives ``timeout``; the enclosing budget
        # must cover both instead of cancelling a valid setup at ~11 seconds.
        return await asyncio.wait_for(_inner(), timeout=(timeout * 2) + 1.0)
    except asyncio.TimeoutError:
        logger.warning(
            "FALLBACK_USED | component=postgres_checkpointer | "
            "from=postgres_build | to=memory_saver | reason=build_timeout | "
            "timeout=%s",
            (timeout * 2) + 1.0,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "FALLBACK_USED | component=postgres_checkpointer | "
            "from=postgres_build | to=memory_saver | reason=build_failed | "
            "err_type=%s | err=%s",
            type(exc).__name__,
            str(exc)[:300],
        )
        return None


async def aclose_postgres_checkpointer(checkpointer: Optional["object"]) -> None:
    """Safely close a checkpointer returned by ``build_postgres_checkpointer``.

    No-op for None / non-Postgres saver.
    """
    if checkpointer is None:
        return
    # FIX-B: 优先关闭 pool(_pg_pool);若仍是旧的单连接 cm(_pg_cm)兼容。
    pool = getattr(checkpointer, "_pg_pool", None)
    try:
        if pool is not None and hasattr(pool, "close"):
            await pool.close()
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres Checkpointer aclose (pool) failed: %s", exc)
    cm = getattr(checkpointer, "_pg_cm", None)
    try:
        if cm is not None:
            await cm.__aexit__(None, None, None)
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres Checkpointer aclose failed: %s", exc)
    try:
        if hasattr(checkpointer, "aclose"):
            await checkpointer.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres Checkpointer aclose (saver) failed: %s", exc)


__all__ = [
    "normalize_postgres_conn_string",
    "probe_postgres_checkpointer",
    "build_postgres_checkpointer",
    "aclose_postgres_checkpointer",
]
