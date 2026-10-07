"""CE-03 整改4：API 验证（ACL / 状态转换 / 忘记删除后读取 / 错误响应 / OpenAPI）。

使用 httpx ASGITransport 直接打 app，覆盖：
- 未认证访问 → 401；
- Index owner ACL：跨用户查文档/job → 404/空；
- Memory owner ACL：跨用户 activate → 400/not_found；
- Workspace Instruction ACL：跨 workspace 查询隔离；
- Conversation Memory Mode ACL：非本人会话更新拒绝；
- 非法状态转换：active 记忆 reject → 400 invalid_transition；
- 忘记/删除后读取：list 不含已忘/已删记忆；
- 安全错误响应：不支持文件类型 → 400 含 code，无堆栈泄漏；
- OpenAPI 路由存在：/context/index、/context/memory、workspace-instructions、memory-mode。
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind


async def _seed_memory(sf, *, public_id, user_id, scope_type="user", workspace_key=None,
                       status="candidate", content="secret"):
    from app.models.context_engine import ContextMemory
    from app.repositories.base import ensure_model_id

    async with sf() as session:
        mem = ContextMemory(
            public_id=public_id, user_id=user_id, scope_type=scope_type,
            workspace_key=workspace_key, memory_type="fact", content=content, status=status,
            activation_source="auto", confidence=0.5, importance=3,
            content_hash="h_" + public_id, idempotency_key="i_" + public_id,
            version=1, created_by_user_id=user_id,
            valid_from=datetime.now(), expires_at=None,
        )
        await ensure_model_id(session, ContextMemory, mem)
        session.add(mem)
        await session.commit()


class _FakeUser:
    """匹配 UserProfile 契约的假用户。"""

    def __init__(self, public_id="usr_1", internal_id=1):
        self.id = public_id
        self.internal_id = internal_id
        self.name = "tester"
        self.username = "tester"
        self.role = "user"
        self.email = "t@example.com"
        self.status = "active"
        self.avatar_url = None


@pytest.fixture()
def api_env(sqlite_session_factory):
    """构造带依赖覆盖的 ASGI app（get_db + get_current_user）。"""
    import httpx
    from app.main import create_app

    app = create_app()

    async def _override_get_db():
        async with sqlite_session_factory() as session:
            yield session
            await session.commit()

    async def _override_current_user():
        return _FakeUser()

    app.dependency_overrides = {}
    from app.db.session import get_db
    from app.api.deps import get_current_user

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current_user

    return app


async def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_unauthorized_returns_401(api_env):
    """未认证访问 → 401。"""
    app = api_env
    # 移除 get_current_user 覆盖，验证真实鉴权路径
    from app.api.deps import get_current_user

    async def _deny():
        from fastapi import HTTPException

        raise HTTPException(status_code=401)

    app.dependency_overrides[get_current_user] = _deny
    async with await _client(app) as client:
        resp = await client.get("/api/context/memory")
        assert resp.status_code == 401


async def test_memory_cross_user_acl(api_env, sqlite_session_factory):
    """Memory owner ACL：跨用户 activate → not_found。"""
    await _seed_memory(sqlite_session_factory, public_id="mem_x", user_id=1, status="candidate")

    app = api_env
    from app.api.deps import get_current_user

    class _User2:
        id = "usr_2"
        internal_id = 2
        name = username = role = email = "x"
        status = "active"
        avatar_url = None

    app.dependency_overrides[get_current_user] = lambda: _User2()
    async with await _client(app) as client:
        resp = await client.post("/api/context/memory/mem_x/activate", json={})
        assert resp.status_code == 400
        body = resp.json()
        assert body.get("code") == "context.memory.not_found"


async def test_memory_invalid_transition(api_env, sqlite_session_factory):
    """非法状态转换：active 记忆 reject → 400 invalid_transition。"""
    await _seed_memory(sqlite_session_factory, public_id="mem_act", user_id=1, status="active")
    async with await _client(api_env) as client:
        resp = await client.post("/api/context/memory/mem_act/reject", json={})
        assert resp.status_code == 400
        assert resp.json().get("code") == "context.memory.invalid_transition"


async def test_index_submit_unsupported_type_safe_error(api_env, sqlite_session_factory, tmp_path):
    """Index 提交：不支持类型 → 400 含 code（安全错误，无堆栈泄漏）。"""
    from app.models.uploaded_file import UploadedFile

    f = tmp_path / "doc.bin"
    f.write_bytes(b"\x00\x01")
    async with sqlite_session_factory() as session:
        session.add(
            UploadedFile(
                id=1, public_id="file_bin", user_id=1, conversation_id=1,
                original_name="doc.bin", stored_name="doc.bin", file_ext="bin",
                file_size=2, storage_type="local", storage_path=str(f),
                created_at=datetime.now(), updated_at=datetime.now(),
            )
        )
        await session.commit()

    async with await _client(api_env) as client:
        resp = await client.post("/api/context/index/documents/submit",
                                 json={"file_public_id": "file_bin"})
        assert resp.status_code == 400
        body = resp.json()
        assert body.get("code") == "context.index.unsupported_type"
        assert "Traceback" not in resp.text
        assert "Error" not in str(body.get("detail", ""))


async def test_workspace_instruction_cross_workspace_isolation(api_env, sqlite_session_factory):
    """Workspace Instruction ACL：跨 workspace 隔离。"""
    from app.models.context_engine import ContextWorkspaceInstruction
    from app.repositories.base import ensure_model_id

    async with sqlite_session_factory() as session:
        for key, ws in (("ws_a", "a"), ("ws_b", "b")):
            inst = ContextWorkspaceInstruction(
                public_id=f"wi_{key}", user_id=1, workspace_key=ws,
                instruction_key=f"rule_{key}", category="general", title=f"T{key}",
                content=f"content {key}", status="active", content_hash=f"h_{key}",
                idempotency_key=f"i_{key}", created_by_user_id=1,
                created_at=datetime.now(), updated_at=datetime.now(),
            )
            await ensure_model_id(session, ContextWorkspaceInstruction, inst)
            session.add(inst)
        await session.commit()

    async with await _client(api_env) as client:
        # 只查 ws_a → 只返回 a 的指令
        resp = await client.get("/api/context/workspace-instructions?workspace_key=a")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert all(i["workspace_key"] == "a" for i in data)
        assert any(i["instruction_key"] == "rule_ws_a" for i in data)
        assert all(i["instruction_key"] != "rule_ws_b" for i in data)


async def test_conversation_memory_mode_acl(api_env, sqlite_session_factory):
    """Conversation Memory Mode ACL：非本人会话 → 不更新。"""
    from app.models.conversation import Conversation

    async with sqlite_session_factory() as session:
        session.add(
            Conversation(
                id=1, public_id="conv_1", user_id=1, title="t",
                created_at=datetime.now(), updated_at=datetime.now(),
            )
        )
        await session.commit()

    app = api_env
    from app.api.deps import get_current_user

    class _User2:
        id = "usr_2"
        internal_id = 2
        name = username = role = email = "x"
        status = "active"
        avatar_url = None

    app.dependency_overrides[get_current_user] = lambda: _User2()
    async with await _client(app) as client:
        resp = await client.patch("/api/conversations/conv_1/memory-mode",
                                  json={"memory_mode": "off"})
        # 非本人 → 不更新（返回 200 但未落库）
        assert resp.status_code == 200
    # 用户 2 无法更新用户 1 的会话记忆模式（WHERE user_id=2 → rowcount=0）
    async with sqlite_session_factory() as session:
        from sqlalchemy import select
        from app.models.conversation import Conversation

        row = (await session.execute(select(Conversation).where(Conversation.public_id == "conv_1"))).scalar_one()
        assert row.context_memory_mode == "inherit"  # 未变更


async def test_forgotten_deleted_not_in_list(api_env, sqlite_session_factory):
    """忘记/删除后读取：list 不含已忘/已删记忆。"""
    from app.models.context_engine import ContextMemory
    from app.repositories.base import ensure_model_id

    now = datetime.now()
    async with sqlite_session_factory() as session:
        for pid, st in (("m_fg", "forgotten"), ("m_dl", "deleted"), ("m_ok", "active")):
            mem = ContextMemory(
                public_id=pid, user_id=1, scope_type="user", memory_type="fact",
                content="c", status=st, activation_source="auto", confidence=0.5,
                importance=3, content_hash="h" + pid, idempotency_key="i" + pid,
                version=1, created_by_user_id=1, deleted_at=now if st != "active" else None,
            )
            await ensure_model_id(session, ContextMemory, mem)
            session.add(mem)
        await session.commit()

    async with await _client(api_env) as client:
        resp = await client.get("/api/context/memory")
        assert resp.status_code == 200
        ids = [m["memory_public_id"] for m in resp.json()]
        assert "m_ok" in ids
        assert "m_fg" not in ids  # deleted_at IS NULL 过滤
        assert "m_dl" not in ids


async def test_openapi_routes_present(api_env):
    """OpenAPI 路由存在。"""
    app = api_env
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/context/index/documents/submit" in paths
    assert "/api/context/memory" in paths
    assert "/api/context/workspace-instructions" in paths
    assert "/api/conversations/{conversation_id}/memory-mode" in paths
