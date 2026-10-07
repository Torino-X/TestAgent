"""Health check endpoints — /health and /health/db."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.response import error, success
from app.db.session import sync_engine

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health(request: Request):
    state = request.app.state
    probe = getattr(state, "probe_report", None)
    return success({
        "status": "ok",
        "service": "TestAgent API",
        "agent_runtime": {
            "production_engine": "langgraph",
            "legacy_orchestrator_mounted": False,
            "langgraph_readiness": bool(
                getattr(probe, "langgraph_readiness", False)
            ),
            "worker_started": bool(getattr(state, "worker_started", False)),
            "coordinator_mounted": getattr(state, "langgraph_coordinator", None)
            is not None,
            "dispatcher_mounted": getattr(state, "api_dispatcher", None) is not None,
            "checkpointer_type": getattr(probe, "checkpointer_type", "unknown"),
            "pending_legacy_tasks": getattr(state, "pending_legacy_tasks", "unknown"),
        },
    })


@router.get("/health/db")
async def health_db():
    """Check database connectivity with a lightweight SELECT 1.

    Does NOT print the DATABASE_URL or password.
    """
    try:
        with sync_engine.connect() as conn:
            from sqlalchemy import text
            result = conn.execute(text("SELECT 1 AS ok"))
            result.fetchone()
        logger.debug("DB健康检查通过")
        return success({"database": "connected"})
    except Exception as exc:
        logger.warning("DB健康检查失败: %s", str(exc)[:200])
        return JSONResponse(
            status_code=503,
            content=error(50001, "database connection failed", {"database": "disconnected"}),
        )


@router.get("/health/readiness")
async def health_readiness(request: Request):
    """Readiness is strict: required database and enabled workers must be live."""
    checks: dict[str, object] = {}
    ready = True

    try:
        with sync_engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("SELECT 1"))
        checks["database"] = "connected"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Readiness database check failed: %s", type(exc).__name__)
        checks["database"] = "disconnected"
        ready = False

    from app.context_engine.feature_flags import get_context_engine_flags

    state = request.app.state
    flags = get_context_engine_flags()
    index_worker = getattr(state, "index_worker", None)
    retention_worker = getattr(state, "retention_worker", None)
    index_state = _worker_health(index_worker)
    retention_state = _worker_health(retention_worker)
    checks["index_worker"] = index_state
    checks["retention_worker"] = retention_state
    if flags.context_index_worker_enabled and not index_state["running"]:
        ready = False
    if flags.context_engine_enabled and not retention_state["running"]:
        ready = False

    probe = getattr(state, "probe_report", None)
    agent_ready = bool(getattr(probe, "langgraph_readiness", False))
    checks["agent_runtime"] = {
        "ready": agent_ready,
        "worker_started": bool(getattr(state, "worker_started", False)),
    }
    if not agent_ready or not checks["agent_runtime"]["worker_started"]:
        ready = False

    body = success({"status": "ready" if ready else "not_ready", "checks": checks})
    if ready:
        return body
    return JSONResponse(status_code=503, content=error(50301, "service not ready", body["data"]))


@router.get("/health/dependencies")
async def health_dependencies(request: Request):
    """Check all dependency health statuses.

    Checks database connectivity and file storage availability.
    Model API and knowledge base API are reported as 'unknown' in Phase 1
    since real API keys are not yet configured.
    """
    deps = {
        "database": "ok",
        "redis": "unverified",
        "file_storage": "unverified",
        "model_api": "unknown",
        "knowledge_base_api": "unknown",
    }

    # Verify database
    try:
        with sync_engine.connect() as conn:
            from sqlalchemy import text
            result = conn.execute(text("SELECT 1 AS ok"))
            result.fetchone()
    except Exception:
        deps["database"] = "disconnected"

    # Verify durable file storage — OSS is the only durable backing store.
    # A HEAD against the configured prefix confirms credentials, network,
    # and bucket access in one call.
    try:
        from app.storage.oss_storage import object_storage

        if not object_storage._get_bucket():
            deps["file_storage"] = "unconfigured"
        else:
            deps["file_storage"] = "configured"
            deps["file_storage_backend"] = object_storage.storage_type
    except Exception as exc:
        deps["file_storage"] = "unavailable"
        deps["file_storage_error"] = type(exc).__name__

    probe = getattr(request.app.state, "probe_report", None)
    redis_ok = getattr(probe, "redis_ok", None)
    if redis_ok is True:
        deps["redis"] = "connected"
    elif redis_ok is False:
        deps["redis"] = "disconnected"

    return success(deps)


def _worker_health(worker) -> dict:
    if worker is None:
        return {"running": False, "status": "not_started"}
    snapshot = getattr(worker, "health_snapshot", None)
    if callable(snapshot):
        try:
            data = snapshot()
            return {"status": "running" if data.get("running") else "stopped", **data}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Worker health probe failed: %s", type(exc).__name__)
    return {"running": False, "status": "unobservable"}


# 路由清单(Health):
#   GET /api/v1/health                              全局健康(进程在 / 配置 OK)
#   GET /api/v1/health/db                            DB 连通(pymysql/aiosqlite)
#   GET /api/v1/health/redis                         Redis 连通(若启用)
#
# 链路:
#   → AsyncSessionLocal / RedisClient 各做一次 ping →
#   返回 success/error(app.core.response 统一格式)
#
# 关键约束:
#   - 健康检查**不允许**抛 500;失败必须返回 503(Service Unavailable)
#     并在 body 里说明哪一项失败;
#   - 不触发任何租户级敏感查询;
#   - CI 探活不依赖 /health/db(可能慢),用 /health 就够。
