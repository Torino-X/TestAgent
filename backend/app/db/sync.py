"""SQLAlchemy session helpers — DB transaction / cache consistency utilities.

P0 收口:任何 cache write-through / invalidate / bump_generation 都必须在
``session.commit()`` 之后执行,否则在事务回滚时 Redis 会留下错误的"领先"
状态(flush 已发到 DB 但 rollback 取消 — 而 Redis 已被 write).

设计:
  * :func:`register_after_commit` 把 cache hook 挂到 SQLAlchemy ``after_commit``
    事件:commit 触发后立即执行;rollback 则 hook 自动跳过.
  * 每次 ``register_after_commit`` 调用挂一个 ``once=True`` 的 listener,
    触发后自动注销.这样:
      - 同一 session 多次注册 → 多次独立触发
      - 同一 hook 不会重复执行

SQLAlchemy Async 注意事项:
  * ``AsyncSession.after_commit`` **不存在** — 必须挂到 ``AsyncSession.sync_session``.
  * Sync listener 在 sync session 上被调用, async hook 必须用 asyncio
    调度:若当前 event loop 可用,``create_task``;否则运行 ``asyncio.run``.
  * 在 FastAPI 路径里, request handler 是 async 的,loop 可用,create_task
    是首选路径.

为什么不在 service 内直接 ``session.flush()`` + cache write:
  * ``flush()`` 只把 SQL 发到 DB,**不保证事务已 commit**.
  * 上层(agent execution worker / api endpoint)可能仍然有 commit/rollback.
  * rollback 后 MySQL 是旧状态,但 Redis 已被写入新状态 — 数据不一致.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def _resolve_sync_target(session: Any) -> Any | None:
    """Return the sync ``Session`` target for event.listen.

    AsyncSession 的事件必须挂到 ``sync_session`` 上.  普通 sync Session
    自身就是 target.  其它(mock/duck) 返回 None.
    """
    if session is None:
        return None
    # AsyncSession has a ``sync_session`` property
    sync_target = getattr(session, "sync_session", None)
    if sync_target is not None:
        return sync_target
    # Plain Session — has ``info`` dict
    if hasattr(session, "info") and hasattr(session, "_flush"):
        return session
    return None


def register_after_commit(
    session: Any,
    hook: Callable[[Any], Awaitable[None] | None],
) -> None:
    """注册一个在 session commit 后才执行的 async hook.

    使用方式::

        async def my_hook(session):
            await cache.write_through(...)

        sync.register_after_commit(session, my_hook)

    Args:
        session: SQLAlchemy ``Session`` 或 ``AsyncSession``.
        hook: 接受 session 参数的可调用对象;可以是 ``async def`` 或普通 def;
              返回值可为 ``None`` 或 ``Awaitable[None]``.

    Notes:
        * Hook 只在 commit 成功后触发;rollback 时不触发.
        * 若 hook 自身抛错,只 log 不传播 — DB 已是已提交状态,不应回滚.
        * 每次 ``register_after_commit`` 调用挂一个 ``once=True`` listener,
          commit 后自动卸载.同一 session 多次注册 → 多次触发.
        * 若 session 已被 close 或不是 SQLAlchemy session,事件不会触发.
    """
    try:
        from sqlalchemy import event  # local import — 避免顶层硬依赖
    except ImportError:
        logger.debug("register_after_commit: SQLAlchemy unavailable; hook skipped")
        return

    sync_target = _resolve_sync_target(session)
    if sync_target is None:
        logger.debug(
            "register_after_commit: session is not SQLAlchemy Session/AsyncSession; "
            "hook not registered. Caller MUST ensure cache write happens after explicit commit."
        )
        return

    def _sync_listener(_session: Any) -> None:
        try:
            result = hook(_session)
            if asyncio.iscoroutine(result):
                # 在 async session 上下文中,我们用 create_task 调度.
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(_safe_await(result))
                else:
                    # 兜底:在 sync 线程内被调用,例如 engine-level commit.
                    # 同步跑:阻塞直到完成.
                    try:
                        asyncio.run(result)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "register_after_commit: asyncio.run fallback failed | %s",
                            exc,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "register_after_commit: hook raised (swallowed) | %s", exc,
            )

    try:
        event.listen(sync_target, "after_commit", _sync_listener, once=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "register_after_commit: event.listen failed (swallowed) | %s", exc,
        )


async def _safe_await(coro: Awaitable[None]) -> None:
    """运行 hook coroutine;失败仅 log."""
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "register_after_commit: async hook raised (swallowed) | %s", exc,
        )


__all__ = ["register_after_commit"]
