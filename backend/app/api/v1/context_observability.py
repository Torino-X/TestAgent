"""CE-05 Context Observability API — metrics/trace/dashboard/alerts。

- GET  /api/v1/context/metrics/overview         （Owner）
- GET  /api/v1/context/metrics/breakdown/{domain}（Owner）
- GET  /api/v1/context/admin/metrics/engine     （Admin global）
- GET  /api/v1/context/trace                    （Owner，必传 task_public_id）
- GET  /api/v1/context/admin/trace              （Admin cross-user）
- GET  /api/v1/context/dashboard/health         （Admin）
- GET  /api/v1/context/alerts                   （Admin）
- POST /api/v1/context/alerts/{alert_id}/ack    （Admin append-only）
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile

router = APIRouter()


def _http_error(status: int, message: str, code: str = "") -> HTTPException:
    from app.core.response import error

    return HTTPException(
        status_code=status,
        detail=error(status * 100, message, {"code": code or f"context.observability.{status}"}),
    )


def _session_factory(session: AsyncSession):
    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


@router.get("/metrics/overview")
async def metrics_overview(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    since_minutes: Optional[int] = Query(default=None, ge=1, le=1440),
):
    """Owner 聚合（user_id 过滤）。"""
    from app.agent_runtime.observability import MetricsService

    service = MetricsService(_session_factory(session))
    data = await service.owner_overview(current.internal_id, since_minutes=since_minutes)
    return success(data)


@router.get("/metrics/breakdown/{domain}")
async def metrics_breakdown(
    domain: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    since_minutes: Optional[int] = Query(default=None, ge=1, le=1440),
):
    """Owner 按 domain 聚合（snapshot|compaction|payload|memory）。"""
    from app.agent_runtime.observability import MetricsService

    if domain not in {"snapshot", "compaction", "payload", "memory"}:
        raise _http_error(400, "domain 参数非法", "context.observability.invalid_domain")
    service = MetricsService(_session_factory(session))
    data = await service.owner_overview(current.internal_id, since_minutes=since_minutes)
    return success({"domain": domain, "data": data})


@router.get("/admin/metrics/engine")
async def metrics_engine_admin(
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    since_minutes: Optional[int] = Query(default=None, ge=1, le=1440),
):
    """Admin 全局 engine 指标（legacy vs langgraph）。"""
    from app.agent_runtime.observability import MetricsService

    service = MetricsService(_session_factory(session))
    data = await service.engine_metrics(since_minutes=since_minutes)
    return success(data)


@router.get("/trace")
async def trace_owner(
    request: Request,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    task_public_id: str = Query(...),
    since_minutes: Optional[int] = Query(default=None, ge=1, le=1440),
    limit: int = Query(default=50, ge=1, le=100),
):
    """Owner Trace：必须带 task_public_id（属当前 user）；只返回 public_id。"""
    from app.agent_runtime.observability import MetricsService

    service = MetricsService(_session_factory(session))
    rows = await service.trace_owner(
        current.internal_id, task_public_id, since_minutes=since_minutes, limit=limit
    )
    return success({"task_public_id": task_public_id, "events": rows})


@router.get("/admin/trace")
async def trace_admin(
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    task_public_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
):
    """Admin 跨用户 Trace（需 task_public_id；写 admin 审计）。"""
    from app.agent_runtime.observability import MetricsService

    service = MetricsService(_session_factory(session))
    # admin 全局：不按 owner user 过滤（跨用户专用 admin 路径）。不能复用
    # trace_owner(user_id=0)，否则会把正常任务错误过滤为空。
    rows = await service.trace_admin(task_public_id, limit=limit)
    return success({"task_public_id": task_public_id, "events": rows})


@router.get("/dashboard/health")
async def dashboard_health(
    request: Request,
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    from app.agent_runtime.observability import MetricsService

    service = MetricsService(_session_factory(session))
    return success(await service.dashboard_health(app_state=request.app.state))


@router.get("/alerts")
async def list_alerts(
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
):
    from app.agent_runtime.observability import AlertService

    service = AlertService(_session_factory(session))
    rows = await service.list_alerts(user_id=current.internal_id, limit=limit)
    return success({"items": rows})


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(
    alert_id: str,
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """append-only ack（不修改原事件）。"""
    from app.agent_runtime.observability import AlertService
    from app.agent_runtime.observability.alert_service import AlertConflict, AlertIntegrityError

    service = AlertService(_session_factory(session))
    try:
        result = await service.ack(
            alert_id=alert_id,
            actor_public_id=current.id,
            user_id=current.internal_id,
        )
    except AlertConflict:
        raise _http_error(409, "同 key 不同 digest 冲突", "context.alert.conflict")
    except AlertIntegrityError:
        raise _http_error(409, "告警数据损坏", "context.alert.data_integrity")
    return success(result)


# 路由清单(CE-05 metrics/trace/dashboard/alerts):
#   GET /api/v1/context/metrics/overview                 (Owner)    Context 使用概览
#   GET /api/v1/context/metrics/breakdown/{domain}       (Owner)    按 domain 拆解
#   GET /api/v1/context/admin/metrics/engine             (Admin)    全局引擎指标
#   GET /api/v1/context/trace                            (Owner)    按 task 看 trace
#   GET /api/v1/context/admin/trace                      (Admin)    跨 user 全局 trace
#   GET /api/v1/context/admin/dashboard                  (Admin)    监控大盘
#   GET /api/v1/context/admin/alerts                     (Admin)    报警列表
#
# 链路:
#   MetricsInMemoryDB / TraceExporterRepository
#     → owner scope → 返回聚合指标
#     → admin scope → 独立鉴权返回全 user 视图
#
# 关键约束:
#   - admin 路径独立鉴权,失败 404(不泄露用户存在性);
#   - trace 仅返回最近 N 条;长时间窗口查询走 dedicated export 接口;
#   - 任何 owner scope 字段绝不暴露 user_id 字段值,只显示 conversation_id;
#   - alerts 是只读,不允许通过此路由 ack(由独立 admin 操作接口处理)。
