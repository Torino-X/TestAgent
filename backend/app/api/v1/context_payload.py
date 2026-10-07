"""CE-05 Context Payload API — 只读元数据 + 授权下载。

- GET /api/v1/context/payloads            （list，owner-scope）
- GET /api/v1/context/payloads/{public_id}（detail，不返回 storage_key/hash/path）
- GET /api/v1/context/payloads/{public_id}/download（流式内容，授权+过期校验）

禁用字段：storage_key / storage_key_hash / storage_path / 内部路径 /
encryption_key_ref。错误：401 / 404（非属主）/ 410（expired/deleted）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile

router = APIRouter()


def _http_error(status: int, code: str, message: str) -> HTTPException:
    from app.core.response import error

    return HTTPException(
        status_code=status,
        detail=error(status * 100, message, {"error_code": code}),
    )


def _iso(dt) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return str(dt)


def _payload_dto(row) -> dict:
    """Payload 只读 DTO（白名单，不含 storage_key/hash/path）。"""
    return {
        "public_id": row.public_id,
        "payload_type": row.payload_type,
        "status": row.status,
        "size_bytes": row.size_bytes,
        "sha256": (row.sha256 or "")[:8] if row.sha256 else None,
        "expires_at": _iso(row.expires_at),
        "created_at": _iso(row.created_at),
    }


async def _get_owned_payload(
    session: AsyncSession,
    user_id: int,
    public_id: str,
):
    from app.models.context_engine import ContextPayload

    return (
        await session.execute(
            select(ContextPayload).where(
                ContextPayload.public_id == public_id,
                ContextPayload.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


def _validate_downloadable_payload(row, *, now: datetime | None = None) -> None:
    """Fail closed before opening storage for deleted, expired, or non-blob rows."""
    effective_now = now or datetime.now(timezone.utc)
    expires_at = getattr(row, "expires_at", None)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=None)
        elif effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)
    status = str(getattr(row, "status", "") or "").lower()
    if getattr(row, "deleted_at", None) is not None or status in {
        "deleted",
        "expired",
        "missing_file",
        "orphan_pending_gc",
    }:
        raise _http_error(410, "context.payload.gone", "载荷已删除或不可用")
    if expires_at is not None and expires_at <= effective_now:
        raise _http_error(410, "context.payload.expired", "载荷已过期")
    if status != "active":
        raise _http_error(410, "context.payload.gone", "载荷状态不可下载")
    if str(getattr(row, "storage_backend", "") or "").lower() == "inline":
        raise _http_error(
            409,
            "context.payload.not_downloadable",
            "该载荷没有可下载的外部对象",
        )


@router.get("/payloads")
async def list_payloads(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    payload_type: Optional[str] = None,
    status: Optional[str] = None,
):
    from app.models.context_engine import ContextPayload

    stmt = select(ContextPayload).where(
        ContextPayload.user_id == current.internal_id
    )
    if payload_type:
        stmt = stmt.where(ContextPayload.payload_type == payload_type)
    if status:
        stmt = stmt.where(ContextPayload.status == status)
    stmt = stmt.order_by(ContextPayload.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return success({"items": [_payload_dto(r) for r in rows]})


@router.get("/payloads/{public_id}")
async def get_payload(
    public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    row = await _get_owned_payload(session, current.internal_id, public_id)
    if row is None:
        raise _http_error(404, "context.payload.not_found", "载荷不存在或不属于当前用户")
    return success(_payload_dto(row))


@router.get("/payloads/{public_id}/download")
async def download_payload(
    public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    # owner-scope 由 storage.open 内部校验（get_by_public_id(public_id, user_id)）
    row = await _get_owned_payload(session, current.internal_id, public_id)
    if row is None:
        raise _http_error(404, "context.payload.not_found", "载荷不存在或不属于当前用户")
    _validate_downloadable_payload(row)

    from app.context_engine.payload.factory import build_payload_backend
    import asyncio

    try:
        backend = build_payload_backend(row.storage_backend)
    except ValueError as exc:
        raise _http_error(
            409,
            "context.payload.backend_unsupported",
            "载荷存储后端不可用",
        ) from exc
    if backend is None:
        raise _http_error(409, "context.payload.not_downloadable", "载荷不可下载")
    try:
        exists = await asyncio.to_thread(
            backend.exists, current.internal_id, row.storage_key
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(
            503, "context.payload.backend_unavailable", "载荷存储暂不可用"
        ) from exc
    if not exists:
        raise _http_error(410, "context.payload.gone", "载荷对象不存在")

    chunk_size = 64 * 1024

    async def _iter():
        data = await asyncio.to_thread(
            backend.read, current.internal_id, row.storage_key
        )
        for offset in range(0, len(data), chunk_size):
            yield data[offset : offset + chunk_size]

    return StreamingResponse(
        _iter(),
        media_type=row.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{row.public_id}.bin"',
        },
    )


# 路由清单(CE-05 Payload 元数据 + 授权下载):
#   GET /api/v1/context/payloads                          (list, owner-scope)
#   GET /api/v1/context/payloads/{public_id}              (detail,no internal path)
#   GET /api/v1/context/payloads/{public_id}/download     (流式内容)
#
# 链路:
#   ContextPayloadRepository.list_for_user / get(public_id)
#     → owner-scope → 校验 → 返回
#     → download → local_storage.open_bytes → StreamingResponse
#
# 关键约束:
#   - detail 永远不返回 storage_key / storage_key_hash / storage_path(白名单
#     字段输出);
#   - download 必须检查 owner + 体积配额 + 过期(若有 TTL);
#   - 大文件走 StreamingResponse,避免一次性 load。
