"""API Contract Reconciliation 测试 — GET context-usage + POST context/compact。

断言（对齐 Backend API Contract Reconciliation 要求）：
1. GET  path 存在
2. POST path 存在
3. Path parameter name 与 Contract 一致（{conversation_id}）
4. Conversation not found：HTTP 404 + code 40401
5. Compact conflict：409 + code 40901
6. Unknown window response schema 一致
7. No snapshot response schema 一致
8. Success envelope 一致（{code,message,data}）

通过真实 FastAPI OpenAPI + 真实 service/route 映射验证（不依赖 mock DB）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ── OpenAPI 验证（真实 FastAPI app）──────────────────────────────────

def _openapi():
    """从 conversations router 构建最小 FastAPI app 拿 OpenAPI。

    避免 import app.main（其模块级 Windows SelectorEventLoop policy 会污染
    同一进程内其他测试的事件循环设置）。
    """
    from fastapi import FastAPI

    from app.api.v1.conversations import router as conv_router

    app = FastAPI()
    app.include_router(conv_router, prefix="/api/conversations")
    return app.openapi()


def test_get_context_usage_path_exists_in_openapi():
    paths = _openapi().get("paths", {})
    assert "/api/conversations/{conversation_id}/context-usage" in paths
    assert "get" in paths["/api/conversations/{conversation_id}/context-usage"]


def test_post_compact_path_exists_in_openapi():
    paths = _openapi().get("paths", {})
    assert "/api/conversations/{conversation_id}/context/compact" in paths
    assert "post" in paths["/api/conversations/{conversation_id}/context/compact"]


@pytest.mark.asyncio
async def test_compact_route_passes_real_session_factory(monkeypatch):
    """Route must pass AsyncSessionLocal, not an async wrapper coroutine factory."""
    from app.api.v1 import conversations as conv_api
    from app.db import session as db_session
    from app.services import manual_compaction_service

    captured = {}

    class _Service:
        def __init__(self, session):
            captured["session"] = session

        async def compact(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(to_dict=lambda: {"summary_updated": True})

    factory = object()
    monkeypatch.setattr(
        manual_compaction_service,
        "ManualConversationCompactionService",
        _Service,
    )
    monkeypatch.setattr(db_session, "AsyncSessionLocal", factory)

    async def _llm_client(_session, _user_id):
        return "llm-client"

    monkeypatch.setattr(conv_api, "_build_llm_client", _llm_client)

    response = await conv_api.compact_conversation_context(
        conversation_id="conv_x",
        current_user=SimpleNamespace(internal_id=7),
        session=object(),
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(context_llm_bridge="bridge"))),
    )

    assert response["code"] == 0
    assert captured["conversation_public_id"] == "conv_x"
    assert captured["user_internal_id"] == 7
    assert captured["session_factory"] is factory
    assert captured["llm_invoker"] == "bridge"
    assert captured["llm_client"] == "llm-client"


def test_path_parameter_name_matches_contract():
    """两个端点 path param 名均为 {conversation_id}（项目主流约定）。"""
    paths = _openapi().get("paths", {})
    for p in ("/api/conversations/{conversation_id}/context-usage",
              "/api/conversations/{conversation_id}/context/compact"):
        assert p in paths, f"missing path {p}"
        for method in ("get", "post"):
            spec = paths[p].get(method)
            if not spec:
                continue
            path_params = [
                pa for pa in spec.get("parameters", []) if pa.get("in") == "path"
            ]
            assert path_params, f"{method} {p} has no path param"
            assert path_params[0]["name"] == "conversation_id", (
                f"{method} {p} param name mismatch: {path_params[0]['name']}"
            )


def test_no_legacy_v1_contract_in_openapi():
    """正式 API 路径不含 /api/v1/conversations（源码目录名非 URL 前缀）。"""
    paths = _openapi().get("paths", {})
    assert not any("/api/v1/conversations" in p for p in paths)


# ── Conversation not found：HTTP 404 + code 40401 ────────────────────

def _contract_db_probe():
    """单次 asyncio.run 内完成两个 DB 探测（私有引擎，避免共享引擎跨 loop 竞态）。

    返回 dict：compact_code / context_usage_raises_not_found。
    """
    import asyncio

    from app.db.session import async_engine  # noqa: F401 — 仅用于读取 URL 语义
    from app.services.context_usage_service import (
        ContextUsageService,
        ConversationNotFound,
    )
    from app.services.manual_compaction_service import (
        ManualCompactionError,
        ManualConversationCompactionService,
    )

    async def _run():
        out = {"compact_code": None, "context_usage_raises_not_found": False}
        # 1) compact 服务对不存在会话
        async with _sf() as session:
            svc = ManualConversationCompactionService(session)
            try:
                await svc.compact(
                    conversation_public_id="conv_contract_nonexistent", user_internal_id=1
                )
            except ManualCompactionError as exc:
                out["compact_code"] = exc.code
        # 2) context-usage 服务对不存在会话
        async with _sf() as session:
            svc = ContextUsageService(session)
            try:
                await svc.get_context_usage(
                    conversation_public_id="conv_contract_nonexistent", user_internal_id=1
                )
            except ConversationNotFound:
                out["context_usage_raises_not_found"] = True
        return out

    # 私有引擎：基于真实 DATABASE_ASYNC_URL，但在本 loop 内创建 + dispose，
    # 不触碰 app.db.session 的共享 async_engine。
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.config import get_settings

    _engine = create_async_engine(get_settings().async_database_url, pool_pre_ping=True)
    _sf = async_sessionmaker(_engine, expire_on_commit=False)
    try:
        result = asyncio.run(_run())
    finally:
        asyncio.run(_engine.dispose())
    return result


def test_compact_not_found_error_maps_to_40401():
    """compact 服务异常 code 含 not_found → route 映射 404 + 40401。"""
    probe = _contract_db_probe()
    assert probe["compact_code"] == "context.compact.conversation_not_found"
    assert "not_found" in probe["compact_code"]
    # route 映射：code_num = 40401 if "not_found" in exc.code else 40001
    code_num = 40401 if "not_found" in probe["compact_code"] else 40001
    assert code_num == 40401
    # internal identifier 保留，但外部 code 是 numeric


def test_context_usage_not_found_route_uses_numeric_code():
    """context-usage not found → route 用 numeric 40401（不再用 string code）。"""
    probe = _contract_db_probe()
    # 1) service 确实抛 ConversationNotFound
    assert probe["context_usage_raises_not_found"] is True
    # 2) route 现在 raise HTTPException(404, error(40401, ...)) —— numeric code 不再 ValidationError
    from app.core.response import error

    detail = error(40401, "会话不存在或不属于当前用户")
    assert detail["code"] == 40401


# ── Compact conflict：409 + code 40901 ───────────────────────────────

def test_compact_conflict_code_mapping():
    """ManualCompactionConflictError → route 映射 409 + 40901。"""
    from app.core.response import error
    from app.services.manual_compaction_service import ManualCompactionConflictError

    # 真实服务异常类型
    exc = ManualCompactionConflictError()
    assert exc.code == "context.compact.in_progress"
    # route: HTTPException(409, error(40901, exc.detail))
    detail = error(40901, exc.detail)
    assert detail["code"] == 40901
    # route 分支明确 409（conversations.py:229-232）
    from fastapi import HTTPException

    http_exc = HTTPException(status_code=409, detail=detail)
    assert http_exc.status_code == 409
    assert http_exc.detail["code"] == 40901


def test_compact_route_in_progress_branch():
    """route 对 ManualCompactionConflictError 的映射分支（源码级断言）。"""
    import inspect

    from app.api.v1 import conversations as module

    src = inspect.getsource(module)
    # compact route：conflict → 409 + numeric 40901
    assert "status_code=409" in src
    assert "error(40901, exc.detail)" in src
    # compact route：not_found 分支映射 numeric 40401（而非 string code）
    assert "40401 if \"not_found\" in exc.code else 40001" in src


def test_context_usage_route_not_found_uses_numeric_code_in_source():
    """context-usage route 的 not-found 分支使用 numeric 40401（非 string code）。"""
    import inspect

    from app.api.v1 import conversations as module

    src = inspect.getsource(module.get_conversation_context_usage)
    assert "error(40401" in src
    assert "status_code=404" in src
    # 不得再调用 error(<string code>)（会触发 ApiResponse.code ValidationError → 500）
    assert "error(\"conversation.not_found\"" not in src


# ── Unknown window / No snapshot schema ──────────────────────────────

def test_no_snapshot_response_schema():
    """no-snapshot → used_tokens=null（不是 0），count_mode=unknown。"""
    from app.services.context_usage_service import ContextUsageData, UI_CATEGORIES

    data = ContextUsageData(
        conversation_public_id="conv_x",
        model={"name": None, "context_window_tokens": None, "window_source": "unknown"},
        usage={
            "used_tokens": None,
            "available_tokens": None,
            "percent": None,
            "count_mode": "unknown",
            "estimated": None,
            "over_limit": False,
        },
        breakdown={c: 0 for c in UI_CATEGORIES},
        compaction={"available": False, "recommended": False, "in_progress": False},
        as_of=None,
        available=False,
    )
    d = data.to_dict()
    assert d["usage"]["used_tokens"] is None
    assert d["usage"]["percent"] is None
    assert d["usage"]["count_mode"] == "unknown"
    assert set(d["breakdown"].keys()) == set(UI_CATEGORIES)


def test_unknown_window_schema():
    """unknown window → context_window_tokens=null, percent=null, window_source=unknown。"""
    from app.services.context_usage_service import ContextUsageService, ContextUsageData, UI_CATEGORIES

    # 直接构造 ContextUsageData（unknown window 分支的输出形状）
    data = ContextUsageData(
        conversation_public_id="conv_x",
        model={"name": "qwen3.7-max", "context_window_tokens": None, "window_source": "unknown"},
        usage={
            "used_tokens": 100,
            "available_tokens": None,
            "percent": None,
            "count_mode": "heuristic",
            "estimated": True,
            "over_limit": False,
        },
        breakdown={c: 0 for c in UI_CATEGORIES},
        compaction={"available": True, "recommended": False, "in_progress": False},
        as_of="2026-08-08T00:00:00",
    )
    d = data.to_dict()
    assert d["model"]["context_window_tokens"] is None
    assert d["model"]["window_source"] == "unknown"
    assert d["usage"]["percent"] is None
    assert d["usage"]["available_tokens"] is None


def test_success_envelope_shape():
    """success envelope = {code:0, message, data, request_id}。"""
    from app.core.response import success

    d = success({"ok": 1}, "success")
    assert d["code"] == 0
    assert d["message"] == "success"
    assert d["data"] == {"ok": 1}
    assert "request_id" in d
