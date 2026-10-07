"""Tests for KnowledgeSearchTool (F017).

Covers:
  * Empty query → invalid input
  * Missing session → recoverable error
  * Missing config → recoverable error
  * Successful retrieval translates hits into legacy shape
  * Disabled-by-config short-circuits with empty payload
  * Simulated failure → recoverable error
  * API key never appears in tool output
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.crypto import encrypt_api_key
from app.tools.knowledge_search_tool import KnowledgeSearchTool


class _FakeRepo:
    def __init__(self, row):
        self._row = row

    async def get_for_user(self, _uid):
        return self._row


class _FakeSession:
    pass


def _row(api_key_encrypted: str | None = None, **overrides) -> SimpleNamespace:
    base = dict(
        api_key_encrypted=api_key_encrypted or encrypt_api_key("sk-" + "test-plain-1234567890"),
        api_base_url="https://kb.example/v1",
        default_knowledge_ids=["kb-1"],
        top_k=5,
        similarity_threshold=0.35,
        retrieve_strategy=3,
        enable_rerank_model=True,
        rerank_model="bge-reranker-v2-m3",
        knowledge_graph=False,
        timeout_seconds=30,
        test_plan_generation_enabled=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _ctx(row=None, session=None):
    ctx = SimpleNamespace(
        task_id="t1", conversation_id="c1", user_id="u1", status="running",
        session=session or _FakeSession(),
        task_internal_id=1, conversation_internal_id=1, user_internal_id=1,
    )
    ctx._repo_factory = lambda _session: _FakeRepo(row)
    # Pre-create the attribute so we can assert it after the run
    ctx.knowledge_search_result = None
    return ctx


@pytest.mark.asyncio
async def test_empty_query_returns_disabled_payload():
    """When the orchestrator does not supply a query (e.g. for a chain
    that does not need KB), the tool must soft-skip rather than
    surface as a failed tool_call — KnowledgeSearchTool is not in
    ``_CORE_TOOLS`` so a failure here only pollutes the UI without
    aborting the task."""
    tool = KnowledgeSearchTool()
    ctx = _ctx(row=_row())
    result = await tool.run({"query": ""}, ctx)
    assert result["success"] is True
    assert result["data"]["disabled_by_config"] is True
    assert result["data"]["skip_reason"] == "query_missing"
    assert result["error"] is None
    assert ctx.knowledge_search_result["disabled_by_config"] is True


@pytest.mark.asyncio
async def test_missing_session_degrades_to_success():
    """BUG FIX 2026-08-18 (方案 2):session 缺失 → 降级 success,不失败任务。
    用户设计明确:知识库未配置/上游失败都不导致整个任务失败。
    测试方案仍基于本地上下文生成。"""
    tool = KnowledgeSearchTool()
    ctx = SimpleNamespace(task_id="t1", user_internal_id=1)
    result = await tool.run({"query": "x"}, ctx)
    assert result["success"] is True
    assert result["data"]["degraded"] is True
    assert result["data"]["error_code"] == "KNOWLEDGE_SESSION_MISSING"


@pytest.mark.asyncio
async def test_missing_config_degrades_to_success(monkeypatch):
    """BUG FIX 2026-08-18 (方案 2):KB 未配置 → 降级 success,不失败任务。
    测试方案仍基于本地上下文生成。"""
    tool = KnowledgeSearchTool()
    ctx = _ctx(row=None)
    # Patch the repo constructor used inside the tool
    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeConfigRepository",
        lambda _session: _FakeRepo(None),
    )
    result = await tool.run({"query": "x"}, ctx)
    assert result["success"] is True
    assert result["data"]["degraded"] is True
    assert result["data"]["error_code"] == "KNOWLEDGE_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_successful_retrieval_translates_hits(monkeypatch):
    tool = KnowledgeSearchTool()
    ctx = _ctx(row=_row())
    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeConfigRepository",
        lambda _session: _FakeRepo(_row()),
    )

    fake_hit = SimpleNamespace(
        knowledge_id="kb-1", doc_id="d1", doc_name="规范",
        chunk_id="c1", chunk_title="条款",
        content="功能测试需覆盖所有模块", score=0.9,
        labels=[], source_type="company_kb",
    )

    async def fake_retrieve(*_a, **_kw):
        return SimpleNamespace(
            success=True, hits=[fake_hit], hit_count=1,
            direct_answer_eligible=True, confidence="high",
            elapsed_ms=7, error_code=None, error_message=None,
        )

    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeRetrievalService",
        lambda _session: SimpleNamespace(retrieve=fake_retrieve),
    )

    result = await tool.run({"query": "x"}, ctx)
    assert result["success"] is True
    assert result["data"]["hit_count"] == 1
    assert result["data"]["confidence"] == "high"
    assert result["data"]["similar_projects"][0]["doc_id"] == "d1"
    assert "规范" in result["data"]["standards"]
    assert ctx.knowledge_search_result["hit_count"] == 1


@pytest.mark.asyncio
async def test_kb_failure_returns_recoverable_error(monkeypatch):
    tool = KnowledgeSearchTool()
    ctx = _ctx(row=_row())
    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeConfigRepository",
        lambda _session: _FakeRepo(_row()),
    )

    async def fake_retrieve(*_a, **_kw):
        return SimpleNamespace(
            success=False, hits=[], hit_count=0,
            direct_answer_eligible=False, confidence="low",
            elapsed_ms=0, error_code="COMPANY_KB_TIMEOUT",
            error_message="upstream timeout",
        )

    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeRetrievalService",
        lambda _session: SimpleNamespace(retrieve=fake_retrieve),
    )

    result = await tool.run({"query": "x"}, ctx)
    assert result["success"] is True
    assert result["data"]["degraded"] is True
    # 上游错误码被透传(degraded payload 用 result.error_code)
    assert result["data"]["error_code"] == "COMPANY_KB_TIMEOUT"


@pytest.mark.asyncio
async def test_disabled_by_config_short_circuits(monkeypatch):
    tool = KnowledgeSearchTool()
    ctx = _ctx(row=_row(test_plan_generation_enabled=False))
    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeConfigRepository",
        lambda _session: _FakeRepo(_row(test_plan_generation_enabled=False)),
    )

    called = {"n": 0}

    async def fake_retrieve(*_a, **_kw):
        called["n"] += 1
        return SimpleNamespace(success=True, hits=[], hit_count=0,
                               direct_answer_eligible=False, confidence="low",
                               elapsed_ms=0, error_code=None, error_message=None)

    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeRetrievalService",
        lambda _session: SimpleNamespace(retrieve=fake_retrieve),
    )

    result = await tool.run({"query": "x"}, ctx)
    assert result["success"] is True
    assert result["data"]["disabled_by_config"] is True
    assert result["data"]["skip_reason"] == "toggled_off"
    assert called["n"] == 0
    assert ctx.knowledge_search_result["disabled_by_config"] is True


@pytest.mark.asyncio
async def test_simulate_failure_degrades_to_success():
    """BUG FIX 2026-08-18 (方案 2):模拟失败 → 降级 success,不失败任务。"""
    tool = KnowledgeSearchTool()
    ctx = _ctx(row=_row())
    result = await tool.run({"query": "x", "_simulate_failure": True}, ctx)
    assert result["success"] is True
    assert result["data"]["degraded"] is True
    assert result["data"]["error_code"] == "KNOWLEDGE_API_UNAVAILABLE"


@pytest.mark.asyncio
async def test_api_key_never_leaks_into_output(monkeypatch):
    tool = KnowledgeSearchTool()
    ctx = _ctx(row=_row(api_key_encrypted="cipher-xxx"))
    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeConfigRepository",
        lambda _session: _FakeRepo(_row(api_key_encrypted="cipher-xxx")),
    )

    fake_hit = SimpleNamespace(
        knowledge_id="kb-1", doc_id="d1", doc_name="规范",
        chunk_id="c1", chunk_title="条款",
        content="x", score=0.9, labels=[], source_type="company_kb",
    )

    async def fake_retrieve(*_a, **_kw):
        return SimpleNamespace(success=True, hits=[fake_hit], hit_count=1,
                               direct_answer_eligible=True, confidence="high",
                               elapsed_ms=1, error_code=None, error_message=None)

    monkeypatch.setattr(
        "app.tools.knowledge_search_tool.KnowledgeRetrievalService",
        lambda _session: SimpleNamespace(retrieve=fake_retrieve),
    )

    result = await tool.run({"query": "x"}, ctx)
    serialised = repr(result) + repr(ctx.knowledge_search_result)
    assert "cipher-xxx" not in serialised