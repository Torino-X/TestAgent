"""Checkpointer 工厂 — Phase 2.8R-C 严格化。

设计要点(对应 docs/35 §3 + 验收三 / 验收四):
  * 工厂函数 ``build_checkpointer`` 接收 ``backend`` 参数:
      - ``backend="memory"`` → ``MemorySaver``(显式,测试用)
      - ``backend="postgres"`` → ``AsyncPostgresSaver``(强制,生产用);
        **失败时抛 ``CheckpointerUnavailableError``**(不再静默 fallback MemorySaver)。
  * ``build_inmemory_checkpointer`` 保留作为向后兼容入口,但内部统一走
    ``build_checkpointer(backend="memory")``。
  * ``build_postgres_checkpointer`` 走 raw SQL,失败抛 CheckpointerUnavailableError,
    由 main.py lifespan 调用。
  * ``aclose_checkpointer`` 统一关资源。

守禁令映射:
  * 守 #18 → LangGraph 任务 Postgres 不可用 → 强制 production_dispatch_forced_off=True
    (probe_router 已实现,但 **silent fallback MemorySaver** 仍存在 → 删除)
  * 守 #21 → 任何 LangGraph 任务不得 downgrade 到 MemorySaver
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.core.exceptions import CheckpointerUnavailableError

logger = logging.getLogger(__name__)


# ── Backward-compatible low-level builders ─────────────────────────


def build_inmemory_checkpointer():
    """构建 ``MemorySaver``(测试或显式声明的 memory backend 用)。

    Production 路径请用 ``build_checkpointer(backend="memory")`` 或
    ``build_checkpointer(backend="postgres", url=...)``。
    """
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


async def build_postgres_checkpointer(
    url: str | None = None,
    *,
    setup: bool = True,
    timeout: float = 10.0,
) -> Any:
    """构建 ``AsyncPostgresSaver``(Phase 2.8A 已有,在这里只 thin-wrap)。

    Phase 2.8R-C:失败抛 ``CheckpointerUnavailableError``(原行为是返回 None + log
    + 让 caller 兜底 MemorySaver,违反守 #18)。
    """
    import asyncio

    from .postgres_checkpointer import build_postgres_checkpointer as _raw_build
    from .postgres_checkpointer import probe_postgres_checkpointer

    target_url = url or os.environ.get("AGENT_RUNTIME_POSTGRES_URL") or ""
    if not target_url:
        raise CheckpointerUnavailableError(
            reason="empty_postgres_url",
            detail={
                "reason": "empty_postgres_url",
                "hint": "set AGENT_RUNTIME_POSTGRES_URL or pass url",
            },
        )

    # 先 probe,再 build
    try:
        ok = await asyncio.wait_for(
            probe_postgres_checkpointer(target_url, timeout=timeout),
            timeout=timeout + 1.0,
        )
    except Exception as exc:
        raise CheckpointerUnavailableError(
            reason="probe_failed",
            detail={
                "reason": "probe_failed",
                "url_echo": _redact_url(target_url),
                "error": str(exc),
            },
        ) from exc

    if not ok:
        raise CheckpointerUnavailableError(
            reason="probe_unhealthy",
            detail={
                "reason": "probe_unhealthy",
                "url_echo": _redact_url(target_url),
            },
        )

    try:
        # Phase 2.8R-D:_raw_build 是 ``async def``,**返回 coroutine**。
        # 必须先 await 拿到真实 saver,不能当成 AsyncContextManager 使用。
        # 历史 bug(2.8R 之前):``cp_cm = _raw_build(...)`` 然后 ``cp = await
        # cp_cm.__aenter__()`` —— coroutine 无 ``__aenter__``,AttributeError
        # 被 except 包成 ``CheckpointerUnavailableError("build_failed")``。
        cp = await _raw_build(target_url, setup=setup, timeout=timeout)
        if cp is None:
            # _raw_build 已经负责调 setup 并返回 saver;None 表示内部失败,
            # 但 raw 版目前实际上只在内部 raise 后返回 None,这里兜底抛错。
            raise CheckpointerUnavailableError(
                reason="build_failed",
                detail={
                    "reason": "build_failed",
                    "url_echo": _redact_url(target_url),
                },
            )
        return cp
    except CheckpointerUnavailableError:
        raise
    except Exception as exc:
        raise CheckpointerUnavailableError(
            reason="build_failed",
            detail={
                "reason": "build_failed",
                "url_echo": _redact_url(target_url),
                "error": str(exc),
            },
        ) from exc


# ── High-level factory ─────────────────────────────────────────────


async def build_checkpointer(
    *,
    backend: str | None = None,
    url: str | None = None,
    timeout: float = 10.0,
) -> Any:
    """统一工厂入口。

    Args:
        backend: ``"memory"`` 或 ``"postgres"``;若 None,根据 env var
            ``AGENT_RUNTIME_CHECKPOINTER_BACKEND`` 决定(默认 ``"memory"``)。
        url: 仅 postgres 用;若 None → env ``AGENT_RUNTIME_POSTGRES_URL``。

    Returns:
        ``MemorySaver`` 或 ``AsyncPostgresSaver`` 实例(后者用 _WrappedCp 包装)。

    Raises:
        CheckpointerUnavailableError: postgres backend 但连接失败。
    """
    chosen = (backend or os.environ.get("AGENT_RUNTIME_CHECKPOINTER_BACKEND") or "memory").strip().lower()

    if chosen in {"", "memory", "inmemory", "in_memory"}:
        return build_inmemory_checkpointer()

    if chosen in {"postgres", "postgresql", "pg"}:
        return await build_postgres_checkpointer(url=url, timeout=timeout)

    raise CheckpointerUnavailableError(
        reason="unknown_backend",
        detail={
            "reason": "unknown_backend",
            "backend": chosen,
            "known": ["memory", "postgres"],
        },
    )


async def aclose_checkpointer(checkpointer: Any | None) -> None:
    """关闭 checkpointer(兼容 Postgres + MemorySaver;None 安全)。

    FIX-B:AsyncPostgresSaver 无 ``aclose`` 方法;若 saver 持有 ``_pg_pool``
    (FIX-B 新增)则关闭 pool。否则退回 ``checkpointer.aclose()``(MemorySaver)。
    """
    if checkpointer is None:
        return
    # FIX-B:优先关闭 pool(若存在)。psycopg_pool.AsyncConnectionPool.close() 是 async。
    pool = getattr(checkpointer, "_pg_pool", None)
    if pool is not None and hasattr(pool, "close"):
        try:
            await pool.close()
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("Checkpointer aclose (pool) skipped: %s", exc)
    try:
        await checkpointer.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Checkpointer aclose skipped: %s", exc)


def _redact_url(url: str) -> str:
    """掩码 URL 中 password,避免日志泄漏。"""
    if not url:
        return ""
    try:
        # postgresql+psycopg://user:pass@host:port/db
        if "://" in url and "@" in url:
            scheme_userpass, host_part = url.split("://", 1)
            if "@" in host_part:
                user_pass, rest = host_part.split("@", 1)
                if ":" in user_pass:
                    user, _ = user_pass.split(":", 1)
                    return f"{scheme_userpass}://{user}:***@{rest}"
        return url
    except Exception:
        return "<redact-failed>"


__all__ = [
    "build_inmemory_checkpointer",
    "build_postgres_checkpointer",
    "build_checkpointer",
    "aclose_checkpointer",
]
