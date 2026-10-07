"""Phase 2.9A.24 — 最终完成态、Artifact交付与completion_summary语义修复。

测试覆盖:
  1. format passed → finalize_task（不经过 generate_completion_summary）
  2. format warning → finalize_task
  3. finalize_task_node 返回 task_status=completed
  4. generate_completion_summary 不再注册到 v3 图中
  5. finalize_task 发布 TASK_COMPLETED payload 含 summary_facts
  6. finalize_task 发布 TASK_COMPLETED payload 含 artifact
  7. finalize_task 发布 TASK_COMPLETED payload 含 format_check
  8. finalize_task 不调用 LLM，优先转发上游已校验的 LLM 摘要
  9. finalize_task 只发布一次 TASK_COMPLETED
  10. graph v3 不包含 generate_completion_summary 节点
  11. fallback_summary_text 不含 waiting_input
  12. build_summary_facts 章节数从 test_plan_content 读取
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


_THIS_FILE = os.path.abspath(__file__)
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(_THIS_FILE))
)


def _strip_comments_and_docstrings(text: str) -> str:
    """Remove comment-only lines and docstrings for code-assertion tests."""
    lines = text.splitlines()
    result = []
    in_docstring = False
    for line in lines:
        stripped = line.strip()
        # Skip comment-only lines
        if stripped.startswith("#"):
            continue
        # Track docstrings (triple-quoted)
        if '"""' in stripped or "'''" in stripped:
            count = stripped.count('"""') + stripped.count("'''")
            if count >= 2:
                # Opening and closing on same line — skip it
                continue
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        result.append(line)
    return "\n".join(result)


class TestFormatCheckRoutesDirectlyToFinalize:
    """§二: 格式检查通过/警告后直接进 finalize_task,不经过 generate_completion_summary。"""

    def _read_routing_after_interrupt(self) -> str:
        path = os.path.join(
            _REPO_ROOT,
            "backend",
            "app",
            "agent_runtime",
            "graphs",
            "test_plan",
            "versions",
            "v3",
            "routing_after_interrupt.py",
        )
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_passed_routes_to_finalize_task(self):
        source = self._read_routing_after_interrupt()
        idx = source.find("def route_after_format_check_for_interrupt")
        assert idx >= 0
        code = _strip_comments_and_docstrings(source[idx:idx + 1000])
        assert "NODE_FINALIZE_TASK" in code
        assert "generate_completion_summary" not in code

    def test_warning_routes_to_finalize_task(self):
        source = self._read_routing_after_interrupt()
        idx = source.find("def route_after_format_check_for_interrupt")
        code = _strip_comments_and_docstrings(source[idx:idx + 1000])
        assert '"passed", "warning"' in code

    def test_accept_routes_to_finalize_task(self):
        source = self._read_routing_after_interrupt()
        idx = source.find("def route_after_format_interrupt")
        assert idx >= 0
        code = _strip_comments_and_docstrings(source[idx:idx + 800])
        assert "generate_completion_summary" not in code
        assert "NODE_FINALIZE_TASK" in code


class TestGenerateCompletionSummaryRemovedFromGraph:
    """§四: generate_completion_summary 不再注册到 v3 图中。"""

    def _read_graph(self) -> str:
        path = os.path.join(
            _REPO_ROOT,
            "backend",
            "app",
            "agent_runtime",
            "graphs",
            "test_plan",
            "versions",
            "v3",
            "graph.py",
        )
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_no_summary_node_registration(self):
        source = self._read_graph()
        code = _strip_comments_and_docstrings(source)
        assert "NODE_SUMMARY" not in code
        assert "generate_completion_summary_node" not in code

    def test_no_summary_in_import(self):
        source = self._read_graph()
        code = _strip_comments_and_docstrings(source)
        assert "NODE_GENERATE_COMPLETION_SUMMARY" not in code


class TestFinalizeTaskNodePayload:
    """§三/六: finalize_task 发布的 TASK_COMPLETED payload 包含完整字段。"""

    def _read_finalize_node(self) -> str:
        """Read only the finalize_task_node function body."""
        path = os.path.join(
            _REPO_ROOT,
            "backend",
            "app",
            "agent_runtime",
            "graphs",
            "test_plan",
            "versions",
            "v3",
            "nodes_post_confirm.py",
        )
        with open(path, encoding="utf-8") as f:
            source = f.read()
        # finalize_task_node is the last async function before __all__
        idx = source.find("async def finalize_task_node")
        assert idx >= 0, "finalize_task_node not found"
        # Find __all__ as end marker
        end_idx = source.find("__all__", idx)
        if end_idx < 0:
            end_idx = len(source)
        return _strip_comments_and_docstrings(source[idx:end_idx])

    def test_finalize_payload_has_summary_facts(self):
        body = self._read_finalize_node()
        assert "summary_facts" in body
        assert "canonical_facts" in body

    def test_finalize_payload_has_artifact(self):
        body = self._read_finalize_node()
        assert '"artifact"' in body

    def test_finalize_payload_has_format_check(self):
        body = self._read_finalize_node()
        assert "format_check" in body

    def test_finalize_prefers_validated_upstream_summary(self):
        body = self._read_finalize_node()
        assert "fallback_summary_text" in body
        assert 'state.get("summary")' in body

    def test_finalize_no_llm_call(self):
        body = self._read_finalize_node()
        assert "LLMClient" not in body
        assert "generate_with_system" not in body

    def test_finalize_emits_task_completed_once(self):
        body = self._read_finalize_node()
        count = body.count("TASK_COMPLETED")
        assert count == 1, f"Expected exactly 1 TASK_COMPLETED emit, found {count}"


class TestSummaryFactsStructure:
    """§六: build_summary_facts 使用真实字段。"""

    def test_build_summary_facts_reads_generated_sections(self):
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {
            "test_plan_content": {
                "generated_sections": [
                    {"title": f"Chapter {i}", "content": f"Content {i}"}
                    for i in range(16)
                ],
                "kept_sections": [],
            },
            "review_result": {
                "passed": True,
                "level": "passed",
                "suggestions": ["建议1", "建议2"],
            },
            "artifact": {
                "public_id": "art_test",
                "file_name": "测试方案.docx",
            },
            "format_check_result": {"status": "passed"},
        }
        facts = build_summary_facts(state)
        assert facts["generated_sections"] == 16
        assert facts["review"]["passed"] is True
        assert facts["artifact"]["present"] is True
        assert facts["artifact"]["format_status"] == "passed"

    def test_fallback_summary_no_waiting_input(self):
        from app.agent_runtime._shared.summary_facts import fallback_summary_text

        state = {
            "test_plan_content": {"generated_sections": [{"title": "T"}]},
            "review_result": {"passed": True, "level": "passed"},
            "artifact": {"public_id": "art_1", "file_name": "test.docx"},
            "format_check_result": {"status": "passed"},
        }
        text = fallback_summary_text(state)
        assert "waiting_input" not in text
        assert "completion_summary" not in text
        assert "1 个章节" in text


class _SummarySink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, **event):
        self.events.append(event)
        return event


class _ValidatedSummaryLLM:
    """A CE-compatible client that returns a valid three-section task summary."""

    is_context_engine_bridge = True

    def __init__(self) -> None:
        self.profiles = []
        self.output_contracts = []

    async def stream_with_system(self, _system_prompt, _user_content, **kwargs):
        self.profiles.append(kwargs.get("llm_task_profile"))
        self.output_contracts.append(kwargs.get("output_contract"))
        yield (
            "<NARRATIVE>### 任务概览\n"
            "预约签到测试方案已完成并已导出。\n\n"
            "### 测试覆盖要点\n"
            "本次内容覆盖预约、签到和审核流程。\n\n"
            "### 审查与待确认\n"
            "- 当前审查未发现需阻断交付的问题。"
            "</NARRATIVE>"
        )


@pytest.mark.asyncio
async def test_task_completed_uses_validated_llm_summary(monkeypatch):
    """LLM summary must survive the narrative node and reach TASK_COMPLETED unchanged."""
    from app.agent.enums import AgentEventType
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_post_confirm

    sink = _SummarySink()
    llm = _ValidatedSummaryLLM()
    flags = SimpleNamespace(
        phase29b_task_summary_narrative_enabled=True,
        phase29b_narrative_timeout_seconds=10,
        phase29b_narrative_repair_attempts=0,
    )
    runtime = SimpleNamespace(
        user_internal_id=1,
        task_internal_id=99,
        event_sink=sink,
        project_context=None,
    )
    state = {
        "task_id": "task-summary-regression",
        "task_status": "exported",
        "graph_run_id": "run-99",
        "completed_nodes": [],
        "test_plan_content": {"generated_sections": [{"title": "项目概述"}]},
        "review_result": {"passed": True, "level": "passed"},
        "artifact": {"public_id": "art-summary", "file_name": "预约签到_测试方案.docx"},
        "format_check_result": {"status": "passed"},
    }
    monkeypatch.setattr(nodes_narrative, "get_feature_flags", lambda: flags)
    monkeypatch.setattr(nodes_narrative, "_resolve_narrative_llm", lambda *_args, **_kwargs: llm)

    narrative_patch = await nodes_narrative.task_summary_narrative_node(state, ctx=runtime)
    assert narrative_patch["task_summary_narrative_result"]["summary_source"] == "llm"
    assert narrative_patch["summary"].startswith("### 任务概览")
    assert llm.profiles[0].name == "task_summary_narrative_composer"
    assert "<NARRATIVE>" in llm.output_contracts[0]

    state.update(narrative_patch)
    await nodes_post_confirm.finalize_task_node(state, ctx=runtime)
    completed = next(
        event for event in sink.events
        if event["event_type"] == AgentEventType.TASK_COMPLETED.value
    )
    assert completed["content"] == narrative_patch["summary"]
    assert completed["payload"]["summary"] == narrative_patch["summary"]
