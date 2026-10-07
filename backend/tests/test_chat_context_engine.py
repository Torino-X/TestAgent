"""WP-BE-09：Normal Chat 正式进入 ContextEngine.compose 的接线验证。

证明：
- 生产 `build_production_context_components` 构建的 engine 注册了全部 Source
  Adapter（此前为空注册表 → compose 会 adapter_not_found）；
- bridge 在 Context Engine 总开关开启时 enabled=True（此前默认 False →
  MIG_CHAT=true 恒 degrade 到 CLARIFY）；
- 普通 chat 的 compose 路径可以真正收集 conversation/memory/rules/rag。
"""

from __future__ import annotations

import pytest


_SNAPSHOT_TABLE_DDL = """
CREATE TABLE llm_context_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id VARCHAR(64) NOT NULL UNIQUE,
    user_id BIGINT NOT NULL,
    conversation_id BIGINT,
    message_id BIGINT,
    agent_task_id BIGINT,
    llm_task_type VARCHAR(64) NOT NULL,
    context_kind VARCHAR(64) NOT NULL,
    included_message_ids TEXT,
    included_file_ids TEXT,
    included_task_ids TEXT,
    included_artifact_ids TEXT,
    included_knowledge_ids TEXT,
    summary_id BIGINT,
    estimated_tokens INTEGER DEFAULT 0,
    context_digest VARCHAR(128),
    context_preview TEXT,
    engine_version VARCHAR(32),
    call_site VARCHAR(128),
    context_profile_key VARCHAR(128),
    context_profile_version VARCHAR(32),
    context_policy_version VARCHAR(32),
    model_config_id BIGINT,
    model_name_snapshot VARCHAR(128),
    context_window_tokens BIGINT,
    input_budget_tokens BIGINT,
    output_reserve_tokens BIGINT,
    runtime_reserve_tokens BIGINT,
    safety_margin_tokens BIGINT,
    target_input_tokens BIGINT,
    estimated_input_tokens BIGINT,
    actual_input_tokens BIGINT,
    actual_output_tokens BIGINT,
    section_stats_json TEXT,
    included_refs_json TEXT,
    dropped_refs_json TEXT,
    retrieval_run_ids_json TEXT,
    compaction_json TEXT,
    tool_output_json TEXT,
    fallback_json TEXT,
    latency_json TEXT,
    prompt_digest CHAR(64),
    prompt_excerpt VARCHAR(2000),
    full_prompt_payload_id BIGINT,
    status VARCHAR(32) DEFAULT 'building',
    error_code VARCHAR(64),
    completed_at DATETIME,
    created_at DATETIME NOT NULL
)
"""


def _isolated_flags(monkeypatch):
    """隔离环境：开总开关 + 关检索（不依赖外部 Qdrant/ES）。"""
    monkeypatch.setenv("CONTEXT_ENGINE_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_ENGINE_AGENT_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_RETRIEVAL_ENABLED", "0")
    monkeypatch.setenv("CONTEXT_LEXICAL_RETRIEVAL_ENABLED", "0")
    monkeypatch.setenv("CONTEXT_DENSE_RETRIEVAL_ENABLED", "0")
    monkeypatch.setenv("QDRANT_HOST", "")
    monkeypatch.setenv("ES_HOST", "")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "")


def test_production_engine_registers_all_source_adapters(monkeypatch):
    """生产 engine 的 source registry 已注册 conversation/memory/rules/evidence。"""
    _isolated_flags(monkeypatch)
    from app.agent_runtime.context_runtime_builder import (
        build_production_context_components,
    )
    from app.context_engine.models.enums import ContextKind

    r = build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )
    assert r["error"] is None
    engine = r["context_engine"]

    registry = engine._source_orchestrator._registry
    for kind in (
        ContextKind.CONVERSATION,
        ContextKind.MEMORY,
        ContextKind.PROJECT_INSTRUCTIONS,
        ContextKind.TASK_STATE,
        ContextKind.EVIDENCE,
    ):
        assert registry.has_kind(kind), f"source adapter missing: {kind}"


def test_production_bridge_enabled_when_engine_on(monkeypatch):
    """Context Engine 总开关开 → bridge.available=True（此前默认 False）。"""
    _isolated_flags(monkeypatch)
    from app.agent_runtime.context_runtime_builder import (
        build_production_context_components,
    )

    r = build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )
    bridge = r["context_llm_bridge"]
    assert bridge.available is True


def test_production_engine_compose_has_memory_and_rules(monkeypatch):
    """chat.reply compose 可收集 memory + project_instructions（真实 adapter）。"""
    import asyncio
    from datetime import datetime
    from types import SimpleNamespace

    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    import app.models  # noqa: F401

    _isolated_flags(monkeypatch)
    monkeypatch.setenv("CONTEXT_MEMORY_READ_ENABLED", "1")

    async def _run():
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _now(dbapi_connection, _):
            dbapi_connection.create_function(
                "NOW", 0, lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )

            def _last_insert_id():
                cur = dbapi_connection.execute("SELECT last_insert_rowid()")
                return cur.fetchone()[0]

            # SQLite 兼容 MySQL 的 LAST_INSERT_ID()（snapshot repo 用）
            dbapi_connection.create_function("LAST_INSERT_ID", 0, _last_insert_id)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sf = async_sessionmaker(engine, expire_on_commit=False)

        # seed conversation + memory + project rule
        from app.models.conversation import Conversation
        from app.models.context_engine import (
            ContextMemory,
            ContextWorkspaceInstruction,
        )
        from app.repositories.base import ensure_model_id

        now = datetime.now().replace(microsecond=0)
        async with sf() as session:
            conv = Conversation(
                public_id="conv_chat9",
                user_id=1,
                title="chat",
                context_workspace_key="conversation:conv_chat9",
                context_memory_mode="inherit",
                created_at=now,
                updated_at=now,
            )
            await ensure_model_id(session, Conversation, conv)
            session.add(conv)
            mem = ContextMemory(
                public_id="mem_chat9",
                user_id=1,
                scope_type="user",
                memory_type="preference",
                content="用户偏好：以后都用中文回答",
                normalized_content="用户偏好：以后都用中文回答",
                status="active",
                activation_source="manual",
                content_hash="m" * 64,
                idempotency_key="idem_chat9",
                created_at=now,
                updated_at=now,
            )
            await ensure_model_id(session, ContextMemory, mem)
            session.add(mem)
            rule = ContextWorkspaceInstruction(
                public_id="rule_chat9",
                user_id=1,
                workspace_key="conversation:conv_chat9",
                instruction_key="api_v3",
                category="constraint",
                title="接口v3",
                content="本项目接口统一使用v3",
                priority=5,
                status="active",
                content_hash="r" * 64,
                idempotency_key="idem_rule9",
                created_by_user_id=1,
                created_at=now,
                updated_at=now,
            )
            await ensure_model_id(session, ContextWorkspaceInstruction, rule)
            session.add(rule)
            await session.commit()

        # 构建 engine：用 sqlite session_factory + stub snapshot writer。
        # snapshot 持久化是 MySQL-only（LAST_INSERT_ID），这里用 stub 仅验证
        # compose 管线（memory + project rules 真实进入 prompt）。
        from app.context_engine.planning.token_counter import TokenCounter
        from app.context_engine.runtime.engine_factory import build_context_engine
        from app.context_engine.sources.production_registry import (
            build_default_source_registry,
        )
        from app.context_engine.models.snapshot_models import ContextSnapshotRef

        class _StubSnapshotWriter:
            async def begin_build(self, command, *, session):
                return ContextSnapshotRef(public_id="cs_stub", status="building")

            async def mark_ready(self, *, session, public_id, user_id):
                return ContextSnapshotRef(public_id=public_id, status="ready")

        engine = build_context_engine(
            source_registry=build_default_source_registry(
                token_counter=TokenCounter(),
                retrieval_enabled=False,
            ),
            snapshot_writer=_StubSnapshotWriter(),
        )

        runtime = SimpleNamespace(
            session_factory=sf,
            user_internal_id=1,
            conversation_internal_id=conv.id,
            context_llm_invoker=None,
        )

        from app.context_engine.models.context import ContextRequest

        request = ContextRequest(
            user_id="user_chat9",
            conversation_id="conv_chat9",
            conversation_public_id="conv_chat9",
            call_site="chat.reply",
            current_user_message="帮我看看项目接口版本",
        )
        composed = await engine.compose(
            request, runtime_context=runtime, execution_mode="active"
        )
        prompt_text = composed.prompt_text or ""
        # memory + project rule 真正进入 compose（证明 adapter 生效）
        assert "以后都用中文回答" in prompt_text or "中文" in prompt_text
        assert "v3" in prompt_text or "接口统一" in prompt_text or "接口v3" in prompt_text
        return True

    assert asyncio.run(_run()) is True
