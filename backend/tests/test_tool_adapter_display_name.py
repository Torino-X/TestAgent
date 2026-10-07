"""Phase 2.9A.X: 后端 _display_tool_name + ResultReviewTool 语义失败事件测试。

覆盖:
A. _display_tool_name 全 8 个工具名映射（修复前只覆盖 5 个）
B. ResultReviewTool level="failed" → emit tool_failed（而非 tool_finished）
C. ResultReviewTool level="passed" / level="warning" → emit tool_finished
D. 其他工具 envelope.success=False 仍按原逻辑 emit tool_failed
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict

import pytest

from app.agent_runtime.adapters.test_agent_tool_adapter import (
    TestAgentToolAdapter,
    _display_tool_name,
)
from app.agent_runtime.cancellation import InMemoryCancellationService
from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.runtime_context import RuntimeContext


# ── A. _display_tool_name 全 8 个工具名 ──────────────────────────────────


@pytest.mark.parametrize(
    ("backend_name", "expected_display"),
    [
        ("RequirementParserTool", "调用Word文档解析工具"),
        ("TemplateParserTool", "调用模板解析工具"),
        ("KnowledgeSearchTool", "调用知识库查询工具"),
        ("SectionSuggestionTool", "调用章节建议工具"),
        ("TestPlanGeneratorTool", "调用测试方案生成工具"),
        # Phase 2.9A.X 新增 3 项 — 修复前会 fallback 到英文名
        ("ResultReviewTool", "调用结果审查工具"),
        ("WordExportTool", "调用Word文档导出工具"),
        ("DocxFormatCheckTool", "调用Word文档格式检查工具"),
    ],
)
def test_display_tool_name_for_all_eight_tools(
    backend_name: str, expected_display: str,
) -> None:
    """全部 8 个标准工具必须映射到中文名,否则前端会显示英文。"""
    assert _display_tool_name(backend_name) == expected_display


def test_display_tool_name_falls_back_to_original_for_unknown() -> None:
    """未知工具名（如动态 Agent）应保持原名,不强行汉化。"""
    assert _display_tool_name("PlanPreparationAgent") == "PlanPreparationAgent"
    assert _display_tool_name("SomeFutureTool") == "SomeFutureTool"


# ── B/C/D. _emit_tool_chunks 事件类型映射 ─────────────────────────────────


class _FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _ReviewResultExecutor:
    """可控的 ResultReviewTool 替身 — 返回任意 data 以模拟审查 level。"""

    def __init__(self, level: str) -> None:
        self._level = level

    async def run(self, _tool_name, _inputs, _context, _retry_context=None):
        return {
            "success": True,  # ResultReviewTool._success 永远 success=True
            "data": {
                "level": self._level,
                "passed": self._level != "failed",
                "issues": [],
                "block_issues": [],
                "review_issues": [],
            },
        }


class _FailingExecutor:
    """真实 envelope.success=False 路径。"""

    async def run(self, _tool_name, _inputs, _context, _retry_context=None):
        return {
            "success": False,
            "error": {"code": "TOOL_RUNTIME_FAILED", "message": "工具崩了"},
        }


def _build_adapter(executor, sink: InMemoryEventSink) -> tuple[TestAgentToolAdapter, RuntimeContext]:
    @asynccontextmanager
    async def session_factory():
        yield _FakeSession()

    runtime = RuntimeContext(
        user_internal_id=1,
        task_internal_id=1,
        conversation_internal_id=1,
        session_factory=session_factory,
        settings_service=object(),
        event_sink=sink,
        cancellation_service=InMemoryCancellationService(),
    )
    adapter = TestAgentToolAdapter(
        tool_executor=executor,
        event_sink=sink,
        session_factory=session_factory,
        clock=datetime.utcnow,
        min_visible_seconds=0,
        transition_seconds=0,
    )
    return adapter, runtime


@pytest.mark.asyncio
async def test_result_review_tool_emits_tool_failed_when_level_is_failed() -> None:
    """ResultReviewTool 即使 envelope.success=True，只要 data.level='failed'
    应当发 tool_failed 事件 — 让前端渲染成红色 FAILED 标签。
    """
    sink = InMemoryEventSink()
    adapter, runtime = _build_adapter(_ReviewResultExecutor(level="failed"), sink)

    await adapter.execute(
        tool_name="ResultReviewTool",
        inputs={},
        ctx_runtime=runtime,
    )

    terminal = [
        e for e in sink.collect()
        if e["event_type"] in ("tool_finished", "tool_failed")
    ]
    assert terminal, "应至少有一个终态事件"
    assert all(e["event_type"] == "tool_failed" for e in terminal), (
        "ResultReviewTool data.level='failed' 必须升级为 tool_failed 事件，"
        f"实际收到 {[e['event_type'] for e in terminal]}"
    )


@pytest.mark.asyncio
async def test_result_review_tool_emits_tool_finished_when_level_is_passed() -> None:
    """ResultReviewTool 审查通过 → 仍是 tool_finished（向后兼容）。"""
    sink = InMemoryEventSink()
    adapter, runtime = _build_adapter(_ReviewResultExecutor(level="passed"), sink)

    await adapter.execute(
        tool_name="ResultReviewTool",
        inputs={},
        ctx_runtime=runtime,
    )

    terminal = [
        e for e in sink.collect()
        if e["event_type"] in ("tool_finished", "tool_failed")
    ]
    assert terminal
    assert all(e["event_type"] == "tool_finished" for e in terminal)


@pytest.mark.asyncio
async def test_result_review_tool_emits_tool_finished_when_level_is_warning() -> None:
    """ResultReviewTool 审查通过但有警告 → 仍是 tool_finished。"""
    sink = InMemoryEventSink()
    adapter, runtime = _build_adapter(_ReviewResultExecutor(level="warning"), sink)

    await adapter.execute(
        tool_name="ResultReviewTool",
        inputs={},
        ctx_runtime=runtime,
    )

    terminal = [
        e for e in sink.collect()
        if e["event_type"] in ("tool_finished", "tool_failed")
    ]
    assert all(e["event_type"] == "tool_finished" for e in terminal)


@pytest.mark.asyncio
async def test_other_tool_envelope_failure_still_emits_tool_failed() -> None:
    """envelope.success=False 路径不受影响 — 任何工具失败仍是 tool_failed。"""
    sink = InMemoryEventSink()
    adapter, runtime = _build_adapter(_FailingExecutor(), sink)

    await adapter.execute(
        tool_name="WordExportTool",
        inputs={},
        ctx_runtime=runtime,
    )

    terminal = [
        e for e in sink.collect()
        if e["event_type"] in ("tool_finished", "tool_failed")
    ]
    assert all(e["event_type"] == "tool_failed" for e in terminal)


# ── E. Payload 透传 display_tool_name — 前端使用 ──────────────────────────


@pytest.mark.asyncio
async def test_tool_started_payload_includes_chinese_display_tool_name() -> None:
    """tool_started 事件 payload 必须带中文 display_tool_name 字段
    — 前端 useTaskEvents.ts 优先读 payload 字段（不是 fallback 到本地映射）。
    """
    sink = InMemoryEventSink()
    adapter, runtime = _build_adapter(_ReviewResultExecutor(level="passed"), sink)

    await adapter.execute(
        tool_name="ResultReviewTool",
        inputs={},
        ctx_runtime=runtime,
    )

    started_events = [e for e in sink.collect() if e["event_type"] == "tool_started"]
    assert started_events, "应有 tool_started 事件"
    payload = started_events[0]["payload"]
    assert payload["display_tool_name"] == "调用结果审查工具", (
        "后端必须在 payload.display_tool_name 直接给出中文名，"
        "前端才能显示为「调用结果审查工具」而不是「ResultReviewTool」"
    )


# ── F. ResultReviewTool level=failed 时 error_summary 分点明细 ──────────


@pytest.mark.asyncio
async def test_result_review_tool_failed_emits_block_issue_summary_in_payload() -> None:
    """Phase 2.9A.X: ResultReviewTool envelope.success=True 但 data.level="failed"
    时, payload.error_summary 必须含分点 block_issues 明细 + 「进入 RepairAgent」,
    而不是通用 fallback「请稍后重试」— 前端 buildToolCallPresentation 会用它。
    """
    sink = InMemoryEventSink()
    # 传 level=failed + 2 个 block_issue
    executor = _ReviewResultExecutor(level="failed")
    # 给一个自定义 data 含 block_issues
    executor._level = "failed"

    class _CustomExecutor:
        async def run(self, _tool_name, _inputs, _context, _retry_context=None):
            return {
                "success": True,
                "data": {
                    "level": "failed",
                    "passed": False,
                    "block_issues": [
                        {"rule_id": "header_mismatch", "section_id": "section_2",
                         "message": "表头键名不符"},
                        {"rule_id": "missing_section", "section_id": "section_5",
                         "message": "缺少必含章节"},
                    ],
                    "issues": [],
                    "review_issues": [],
                },
            }

    adapter, runtime = _build_adapter(_CustomExecutor(), sink)

    await adapter.execute(
        tool_name="ResultReviewTool",
        inputs={},
        ctx_runtime=runtime,
    )

    terminal = [e for e in sink.collect() if e["event_type"] == "tool_failed"]
    assert terminal, "level=failed 应触发 tool_failed 事件"
    payload = terminal[0]["payload"]
    error_summary = payload.get("error_summary", "")
    assert "审查未通过" in error_summary
    assert "1." in error_summary and "2." in error_summary
    assert "RepairAgent" in error_summary, (
        "error_summary 必须提示「接下来进入 RepairAgent」"
    )
    # 不应出现「请稍后重试」误导文案
    assert "请稍后重试" not in error_summary


@pytest.mark.asyncio
async def test_other_tool_envelope_failure_no_longer_says_retry() -> None:
    """通用 fallback 文案不再用「请稍后重试」(误导用户),改为「请查看后端日志」。"""
    from app.agent_runtime.adapters.test_agent_tool_adapter import (
        _safe_tool_error_summary,
    )
    summary = _safe_tool_error_summary("UnknownToolXYZ", {"code": "INTERNAL"})
    assert "请稍后重试" not in summary, (
        f"通用 fallback 不应再说「请稍后重试」误导用户: {summary!r}"
    )
    assert "请查看后端日志" in summary


class _KnowledgeResultExecutor:
    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    async def run(self, _tool_name, _inputs, _context, _retry_context=None):
        return {
            "success": True,
            "data": self._data,
            "summary": "知识库检索已结束",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {
                "disabled_by_config": True,
                "skip_reason": "not_configured",
                "error_code": "KNOWLEDGE_NOT_CONFIGURED",
            },
            "公司知识库未配置，已跳过",
        ),
        ({"chunks": [], "hit_count": 0}, "公司知识库已查询，但未命中可引用内容"),
        (
            {"degraded": True, "skip_reason": "upstream_unavailable"},
            "公司知识库暂不可用，已降级继续",
        ),
    ],
)
async def test_knowledge_search_completion_message_reflects_real_source_status(
    data: Dict[str, Any], expected: str,
) -> None:
    """Company RAG 的可继续降级不应被渲染为“结果已整理完成”。"""
    sink = InMemoryEventSink()
    adapter, runtime = _build_adapter(_KnowledgeResultExecutor(data), sink)

    await adapter.execute(
        tool_name="KnowledgeSearchTool",
        inputs={"query": "准入规则"},
        ctx_runtime=runtime,
    )

    finished = [event for event in sink.collect() if event["event_type"] == "tool_finished"]
    assert finished
    assert expected in finished[-1]["payload"]["completion_message"]
    assert "知识库查询结果已整理完成" not in finished[-1]["payload"]["completion_message"]


@pytest.mark.asyncio
async def test_knowledge_search_completion_message_includes_project_rag_status() -> None:
    sink = InMemoryEventSink()
    adapter, runtime = _build_adapter(
        _KnowledgeResultExecutor(
            {
                "disabled_by_config": True,
                "skip_reason": "not_configured",
                "error_code": "KNOWLEDGE_NOT_CONFIGURED",
            }
        ),
        sink,
    )

    await adapter.execute(
        tool_name="KnowledgeSearchTool",
        inputs={"query": "准入规则"},
        ctx_runtime=runtime,
        graph_state={
            "_project_rag_display": {
                "status": "completed",
                "hits": [{"evidence_id": "project_chunk_1"}],
            }
        },
    )

    finished = [event for event in sink.collect() if event["event_type"] == "tool_finished"]
    assert finished
    message = finished[-1]["payload"]["completion_message"]
    assert "公司知识库未配置，已跳过" in message
    assert "\n项目资料检索已完成，命中 1 条可引用内容。" in message
