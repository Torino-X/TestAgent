"""Dynamic Agent v1 graph skeleton.

Phase-1 intentionally provides only a fixed, checkpointable graph shell.
Planner/executor/verifier nodes are added in later phases without changing
TestPlan graph topology.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.atomic_capability_registry import AtomicCapabilityRegistry
from app.agent_runtime.dynamic_agent.executor import DynamicStepExecutor
from app.agent_runtime.dynamic_agent.plan_validator import DynamicPlanValidator
from app.agent_runtime.dynamic_agent.planner import DynamicPlanner
from app.agent_runtime.dynamic_agent.replanner import DynamicReplanner
from app.agent_runtime.dynamic_agent.schemas import (
    DynamicPlan,
    DynamicPlanStep,
    DynamicStepStatus,
)
from app.agent_runtime.dynamic_agent.synthesizer import DynamicSynthesizer
from app.agent_runtime.dynamic_agent.verifier import (
    DynamicVerifier,
    VerificationDecision,
)
from app.agent_runtime._shared.public_narrative import normalize_public_update
from .constants import GRAPH_NAME_DYNAMIC_AGENT, GRAPH_VERSION_DYNAMIC_AGENT_V1
from .state import DynamicAgentState

logger = logging.getLogger(__name__)

NODE_INITIALIZE = "initialize"
NODE_CREATE_PLAN = "create_plan"
NODE_VALIDATE_PLAN = "validate_plan"
NODE_EXECUTE_NEXT_STEP = "execute_next_step"
NODE_VERIFY_GOAL = "verify_goal"
NODE_REPLAN = "replan"
NODE_SYNTHESIZE = "synthesize"
NODE_PERSIST_FINAL_ANSWER = "persist_final_answer"
NODE_NEED_USER = "need_user"
NODE_FINALIZE = "finalize"
NODE_FAIL = "fail"


def initialize(state: DynamicAgentState) -> dict:
    completed = list(state.get("completed_nodes") or [])
    if NODE_INITIALIZE not in completed:
        completed.append(NODE_INITIALIZE)
    return {
        "current_node": NODE_INITIALIZE,
        "current_phase": "initialized",
        "task_status": "running",
        "plan_revision": int(state.get("plan_revision") or 0),
        "replan_count": int(state.get("replan_count") or 0),
        "tool_call_count": int(state.get("tool_call_count") or 0),
        "completed_nodes": completed,
    }


async def plan_node(state: DynamicAgentState, config: Any = None) -> dict:
    completed = _completed(state, NODE_CREATE_PLAN)
    if state.get("plan"):
        return {"completed_nodes": completed}
    if _needs_attachment_clarification(state):
        plan = DynamicPlan(
            goal=str(state.get("goal") or state.get("dynamic_goal") or "选择附件"),
            revision=int(state.get("plan_revision") or 1),
            steps=[
                DynamicPlanStep(
                    step_id="step_1",
                    title="确认要分析的附件",
                    action_type="need_user",
                    capability_key="evidence_analysis",
                    input_refs=[],
                    depends_on=[],
                    success_criteria=["attachment_selection_received"],
                    status=DynamicStepStatus.WAITING_USER,
                )
            ],
        )
        clarification = {
            "question": "当前有多个可分析的 Word 文档，请确认要分析哪一个。",
            "required_input": "attachment_selection",
            "attachments": _attachment_options(state),
        }
        await _emit_dynamic_event(
            state,
            config,
            event_type="plan_created",
            title="需要确认附件",
            content=clarification["question"],
            payload={
                "plan": plan.model_dump(mode="python"),
                "revision": plan.revision,
                "plan_revision": plan.revision,
                "goal": str(state.get("goal") or state.get("dynamic_goal") or ""),
                "target_capability": str(state.get("target_capability") or ""),
                "operation": str(state.get("operation") or ""),
                "understanding_summary": _understanding_summary(state),
                "steps": _display_plan_steps(plan, include_terminal_steps=False),
            },
            node_name=NODE_CREATE_PLAN,
        )
        return {
            "plan": plan.model_dump(mode="python"),
            "plan_revision": plan.revision,
            "awaiting_user": True,
            "clarification": clarification,
            "completed_nodes": completed,
        }
    planner = DynamicPlanner(AtomicCapabilityRegistry.default())
    plan = await planner.aplan(dict(state), runtime_context=_runtime_context(config))
    await _emit_dynamic_event(
        state,
        config,
        event_type="plan_created",
        title="制定计划",
        content="已根据当前目标生成动态执行计划。",
        payload={
            "plan": plan.model_dump(mode="python"),
            "revision": plan.revision,
            "plan_revision": plan.revision,
            "goal": str(state.get("goal") or state.get("dynamic_goal") or ""),
            "target_capability": str(state.get("target_capability") or ""),
            "operation": str(state.get("operation") or ""),
            "understanding_summary": _understanding_summary(state),
            "steps": _display_plan_steps(plan),
        },
        node_name=NODE_CREATE_PLAN,
    )
    return {
        "plan": plan.model_dump(mode="python"),
        "plan_revision": plan.revision,
        "completed_nodes": completed,
    }


def validate_plan_node(state: DynamicAgentState) -> dict:
    completed = _completed(state, NODE_VALIDATE_PLAN)
    try:
        plan = DynamicPlan.model_validate(state.get("plan"))
    except Exception as exc:
        return _failure("PLAN_SCHEMA_ERROR", [str(exc)], completed)
    result = DynamicPlanValidator(AtomicCapabilityRegistry.default()).validate(
        plan, dict(state)
    )
    if not result.valid:
        return _failure("PLAN_VALIDATION_ERROR", result.errors, completed)
    return {
        "failure": None,
        "task_status": "running",
        "completed_nodes": completed,
    }


async def execute_next_step_node(state: DynamicAgentState, config: Any = None) -> dict:
    completed = _completed(state, NODE_EXECUTE_NEXT_STEP)
    plan = DynamicPlan.model_validate(state.get("plan"))
    step = _next_pending_step(plan)
    if step is None:
        return {"completed_nodes": completed}

    running_step = step.model_copy(update={"status": DynamicStepStatus.RUNNING})
    plan = _replace_step(plan, running_step)
    await _emit_dynamic_event(
        state,
        config,
        event_type="plan_step_started",
        title=running_step.title,
        content=running_step.title,
        payload={
            "step_id": running_step.step_id,
            "capability_key": running_step.capability_key,
            "plan_revision": plan.revision,
        },
        node_name=NODE_EXECUTE_NEXT_STEP,
    )
    executor = DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_runtime_context(config),
        file_resolver=_dynamic_file_resolver(config),
    )
    observation = await executor.execute(running_step, dict(state))
    next_status = (
        DynamicStepStatus.COMPLETED
        if observation.status == "success"
        else DynamicStepStatus.FAILED
    )
    finished_step = running_step.model_copy(update={"status": next_status})
    plan = _replace_step(plan, finished_step)
    await _emit_dynamic_event(
        state,
        config,
        event_type=(
            "plan_step_completed"
            if next_status == DynamicStepStatus.COMPLETED
            else "plan_step_failed"
        ),
        title=finished_step.title,
        content=observation.summary,
        payload={
            "step_id": finished_step.step_id,
            "capability_key": finished_step.capability_key,
            "status": next_status.value,
            "plan_revision": plan.revision,
            "observation_id": observation.observation_id,
        },
        node_name=NODE_EXECUTE_NEXT_STEP,
    )

    observations = list(state.get("observations") or [])
    observations.append(observation.model_dump(mode="python"))
    tool_results = dict(state.get("tool_results") or {})
    tool_results[step.step_id] = {
        "status": observation.status,
        "summary": observation.summary,
        "facts": observation.facts,
        "data": observation.data,
    }
    analysis_results = dict(state.get("analysis_results") or {})
    if step.capability_key == "evidence_analysis" and observation.status == "success":
        analysis_results[step.step_id] = observation.summary

    return {
        "plan": plan.model_dump(mode="python"),
        "current_step_id": step.step_id,
        "observations": observations,
        "tool_results": tool_results,
        "analysis_results": analysis_results,
        "tool_call_count": int(state.get("tool_call_count") or 0) + (
            1 if step.action_type == "tool" else 0
        ),
        "completed_nodes": completed,
    }


async def verify_goal_node(state: DynamicAgentState, config: Any = None) -> dict:
    completed = _completed(state, NODE_VERIFY_GOAL)
    await _emit_dynamic_event(
        state,
        config,
        event_type="plan_step_started",
        title="检查分析结果",
        content="正在检查当前分析结果是否满足用户任务。",
        payload={
            "step_id": "verify_goal",
            "status": DynamicStepStatus.RUNNING.value,
            "plan_revision": int(state.get("plan_revision") or 1),
        },
        node_name=NODE_VERIFY_GOAL,
    )
    await _emit_dynamic_public_update(
        state,
        config,
        title="\u68c0\u67e5\u5206\u6790\u7ed3\u679c",
        headline="\u6b63\u5728\u68c0\u67e5\u5206\u6790\u7ed3\u679c",
        summary="\u6211\u4f1a\u5bf9\u7167\u4f60\u7684\u4efb\u52a1\u76ee\u6807\uff0c\u68c0\u67e5\u5f53\u524d\u6587\u6863\u5206\u6790\u662f\u5426\u8db3\u591f\u56de\u7b54\u95ee\u9898\u3002",
        impact="\u5982\u679c\u53d1\u73b0\u8bc1\u636e\u4e0d\u8db3\uff0c\u7cfb\u7edf\u4f1a\u8fdb\u5165\u8865\u5145\u5206\u6790\u6216\u91cd\u65b0\u89c4\u5212\u3002",
        next_action="\u68c0\u67e5\u901a\u8fc7\u540e\u5c06\u6574\u7406\u6700\u7ec8\u7b54\u590d\u3002",
        step_id="verify_goal",
        step_title="检查分析结果",
        step_index=3,
        node_name=NODE_VERIFY_GOAL,
    )
    result = DynamicVerifier().verify(dict(state))
    await _emit_dynamic_event(
        state,
        config,
        event_type="plan_step_completed",
        title="检查分析结果",
        content="已检查当前分析结果。",
        payload={
            "step_id": "verify_goal",
            "status": DynamicStepStatus.COMPLETED.value,
            "verification_status": result.decision.value,
            "verification_gaps": list(result.gaps),
            "plan_revision": int(state.get("plan_revision") or 1),
        },
        node_name=NODE_VERIFY_GOAL,
    )
    return {
        "verification_status": result.decision.value,
        "verification_gaps": list(result.gaps),
        "completed_nodes": completed,
    }


async def replan_node(state: DynamicAgentState, config: Any = None) -> dict:
    completed = _completed(state, NODE_REPLAN)
    replan_count = int(state.get("replan_count") or 0)
    if replan_count >= 2:
        return _failure(
            "REPLAN_BUDGET_EXHAUSTED",
            list(state.get("verification_gaps") or []),
            completed,
        )
    plan = DynamicPlan.model_validate(state.get("plan"))
    next_plan = DynamicReplanner().replan(
        plan,
        gaps=list(state.get("verification_gaps") or []),
    )
    await _emit_dynamic_event(
        state,
        config,
        event_type="plan_updated",
        title="更新计划",
        content="已根据执行结果更新后续计划。",
        payload={
            "plan": next_plan.model_dump(mode="python"),
            "revision": next_plan.revision,
            "plan_revision": next_plan.revision,
            "replan_count": replan_count + 1,
            "steps": _display_plan_steps(next_plan),
        },
        node_name=NODE_REPLAN,
    )
    return {
        "plan": next_plan.model_dump(mode="python"),
        "plan_revision": next_plan.revision,
        "replan_count": replan_count + 1,
        "verification_status": None,
        "completed_nodes": completed,
    }


async def synthesize_node(state: DynamicAgentState, config: Any = None) -> dict:
    completed = _completed(state, NODE_SYNTHESIZE)
    await _emit_dynamic_event(
        state,
        config,
        event_type="plan_step_started",
        title="生成最终答复",
        content="正在整理文档分析结论。",
        payload={
            "step_id": "synthesize_answer",
            "status": DynamicStepStatus.RUNNING.value,
            "plan_revision": int(state.get("plan_revision") or 1),
        },
        node_name=NODE_SYNTHESIZE,
    )
    await _emit_dynamic_public_update(
        state,
        config,
        title="\u751f\u6210\u6700\u7ec8\u7b54\u590d",
        headline="\u6b63\u5728\u751f\u6210\u6700\u7ec8\u7b54\u590d",
        summary="\u6211\u4f1a\u5c06\u5df2\u901a\u8fc7\u68c0\u67e5\u7684\u6587\u6863\u5206\u6790\u7ed3\u679c\u6574\u7406\u6210\u53ef\u76f4\u63a5\u9605\u8bfb\u7684\u56de\u7b54\u3002",
        impact="\u8fd9\u4e00\u6b65\u4e0d\u751f\u6210\u65b0\u6587\u4ef6\uff0c\u53ea\u8f93\u51fa\u672c\u6b21\u95ee\u7b54\u7684\u6700\u7ec8\u7ed3\u8bba\u3002",
        next_action="\u7b54\u590d\u751f\u6210\u540e\u5c06\u7ed3\u675f\u672c\u6b21\u4efb\u52a1\u3002",
        step_id="synthesize_answer",
        step_title="生成最终答复",
        step_index=4,
        node_name=NODE_SYNTHESIZE,
    )
    answer = DynamicSynthesizer().synthesize(dict(state))
    await _emit_dynamic_event(
        state,
        config,
        event_type="plan_step_completed",
        title="生成最终答复",
        content="已整理最终答复。",
        payload={
            "step_id": "synthesize_answer",
            "status": DynamicStepStatus.COMPLETED.value,
            "plan_revision": int(state.get("plan_revision") or 1),
        },
        node_name=NODE_SYNTHESIZE,
    )
    return {
        "final_answer": answer,
        "completed_nodes": completed,
    }


async def need_user_node(state: DynamicAgentState, config: Any = None) -> dict:
    completed = _completed(state, NODE_NEED_USER)
    clarification = (
        state.get("clarification")
        if isinstance(state.get("clarification"), dict)
        else {}
    )
    question = str(clarification.get("question") or "需要你补充信息后继续。")
    await _emit_dynamic_event(
        state,
        config,
        event_type="task_waiting",
        title="等待用户补充",
        content=question,
        payload={
            "pause_marker": "dynamic_agent_need_user",
            "status": "waiting_user",
        },
        node_name=NODE_NEED_USER,
    )
    await _emit_dynamic_event(
        state,
        config,
        event_type="need_user_confirm",
        title="需要确认",
        content=question,
        payload={
            "question": question,
            "confirmation_type": str(
                clarification.get("required_input") or "user_input"
            ),
            "required_input": str(clarification.get("required_input") or "user_input"),
            "attachments": list(clarification.get("attachments") or []),
        },
        node_name=NODE_NEED_USER,
    )
    return {
        "task_status": "waiting_user",
        "current_node": NODE_NEED_USER,
        "current_phase": "waiting_user",
        "pause_marker": "dynamic_agent_need_user",
        "awaiting_user": True,
        "verification_status": VerificationDecision.NEED_USER.value,
        "verification_gaps": ["user_input_required"],
        "completed_nodes": completed,
    }


def persist_final_answer_node(state: DynamicAgentState) -> dict:
    completed = _completed(state, NODE_PERSIST_FINAL_ANSWER)
    return {
        "final_answer": state.get("final_answer") or "",
        "completed_nodes": completed,
    }


async def finalize_node(state: DynamicAgentState, config: Any = None) -> dict:
    completed = _completed(state, NODE_FINALIZE)
    final_answer = str(state.get("final_answer") or "").strip()
    await _emit_dynamic_event(
        state,
        config,
        event_type="task_completed",
        title="任务完成",
        content=final_answer,
        payload={
            "summary": final_answer,
            "final_answer": final_answer,
            "status": "completed",
        },
        node_name=NODE_FINALIZE,
    )
    return {
        "task_status": "completed",
        "current_node": NODE_FINALIZE,
        "current_phase": "completed",
        "completed_nodes": completed,
    }


def fail_node(state: DynamicAgentState) -> dict:
    completed = _completed(state, NODE_FAIL)
    failure = state.get("failure") or {"code": "DYNAMIC_AGENT_FAILED", "errors": []}
    return {
        "task_status": "failed",
        "current_node": NODE_FAIL,
        "current_phase": "failed",
        "failure": failure,
        "completed_nodes": completed,
    }


def build_dynamic_agent_v1_graph(checkpointer=None):
    graph = StateGraph(DynamicAgentState)
    graph.add_node(NODE_INITIALIZE, initialize)
    graph.add_node(NODE_CREATE_PLAN, plan_node)
    graph.add_node(NODE_VALIDATE_PLAN, validate_plan_node)
    graph.add_node(NODE_EXECUTE_NEXT_STEP, execute_next_step_node)
    graph.add_node(NODE_VERIFY_GOAL, verify_goal_node)
    graph.add_node(NODE_REPLAN, replan_node)
    graph.add_node(NODE_SYNTHESIZE, synthesize_node)
    graph.add_node(NODE_PERSIST_FINAL_ANSWER, persist_final_answer_node)
    graph.add_node(NODE_NEED_USER, need_user_node)
    graph.add_node(NODE_FINALIZE, finalize_node)
    graph.add_node(NODE_FAIL, fail_node)
    graph.add_edge(START, NODE_INITIALIZE)
    graph.add_edge(NODE_INITIALIZE, NODE_CREATE_PLAN)
    graph.add_edge(NODE_CREATE_PLAN, NODE_VALIDATE_PLAN)
    graph.add_conditional_edges(
        NODE_VALIDATE_PLAN,
        route_after_validate_plan,
        path_map={
            NODE_EXECUTE_NEXT_STEP: NODE_EXECUTE_NEXT_STEP,
            NODE_NEED_USER: NODE_NEED_USER,
            NODE_FAIL: NODE_FAIL,
        },
    )
    graph.add_conditional_edges(
        NODE_EXECUTE_NEXT_STEP,
        route_after_execute,
        path_map={NODE_EXECUTE_NEXT_STEP: NODE_EXECUTE_NEXT_STEP, NODE_VERIFY_GOAL: NODE_VERIFY_GOAL},
    )
    graph.add_conditional_edges(
        NODE_VERIFY_GOAL,
        route_after_verify,
        path_map={
            NODE_SYNTHESIZE: NODE_SYNTHESIZE,
            NODE_REPLAN: NODE_REPLAN,
            NODE_EXECUTE_NEXT_STEP: NODE_EXECUTE_NEXT_STEP,
            NODE_NEED_USER: NODE_NEED_USER,
            NODE_FAIL: NODE_FAIL,
        },
    )
    graph.add_conditional_edges(
        NODE_REPLAN,
        route_after_replan,
        path_map={NODE_EXECUTE_NEXT_STEP: NODE_EXECUTE_NEXT_STEP, NODE_FAIL: NODE_FAIL},
    )
    graph.add_edge(NODE_SYNTHESIZE, NODE_PERSIST_FINAL_ANSWER)
    graph.add_edge(NODE_PERSIST_FINAL_ANSWER, NODE_FINALIZE)
    graph.add_edge(NODE_NEED_USER, END)
    graph.add_edge(NODE_FINALIZE, END)
    graph.add_edge(NODE_FAIL, END)
    return graph.compile(
        checkpointer=checkpointer,
        name=f"{GRAPH_NAME_DYNAMIC_AGENT}_{GRAPH_VERSION_DYNAMIC_AGENT_V1}",
    )


def route_after_validate_plan(state: DynamicAgentState) -> str:
    if state.get("awaiting_user") or isinstance(state.get("clarification"), dict):
        return NODE_NEED_USER
    return NODE_FAIL if state.get("failure") else NODE_EXECUTE_NEXT_STEP


def route_after_execute(state: DynamicAgentState) -> str:
    plan = DynamicPlan.model_validate(state.get("plan"))
    return NODE_EXECUTE_NEXT_STEP if _next_pending_step(plan) is not None else NODE_VERIFY_GOAL


def route_after_verify(state: DynamicAgentState) -> str:
    status = state.get("verification_status")
    if status == VerificationDecision.COMPLETE.value:
        return NODE_SYNTHESIZE
    if status == VerificationDecision.CONTINUE.value:
        return NODE_EXECUTE_NEXT_STEP
    if status == VerificationDecision.REPLAN.value:
        return NODE_REPLAN
    if status == VerificationDecision.NEED_USER.value:
        return NODE_NEED_USER
    return NODE_FAIL


def route_after_replan(state: DynamicAgentState) -> str:
    return NODE_FAIL if state.get("failure") else NODE_EXECUTE_NEXT_STEP


def _completed(state: DynamicAgentState, node: str) -> list[str]:
    completed = list(state.get("completed_nodes") or [])
    if node not in completed:
        completed.append(node)
    return completed


def _failure(code: str, errors: list[str], completed: list[str]) -> dict:
    return {
        "task_status": "failed",
        "failure": {"code": code, "errors": errors},
        "completed_nodes": completed,
    }


def _next_pending_step(plan: DynamicPlan) -> DynamicPlanStep | None:
    completed = {
        step.step_id
        for step in plan.steps
        if str(step.status) in {"completed", "skipped", "superseded"}
    }
    for step in plan.steps:
        if str(step.status) != "pending":
            continue
        if all(dep in completed for dep in step.depends_on):
            return step
    return None


def _replace_step(plan: DynamicPlan, replacement: DynamicPlanStep) -> DynamicPlan:
    return plan.model_copy(
        update={
            "steps": [
                replacement if step.step_id == replacement.step_id else step
                for step in plan.steps
            ]
        }
    )


def _runtime_context(config: Any) -> Any:
    configurable = (config or {}).get("configurable") if isinstance(config, dict) else {}
    return (configurable or {}).get("runtime_context")


def _dynamic_file_resolver(config: Any) -> Any:
    configurable = (config or {}).get("configurable") if isinstance(config, dict) else {}
    return (configurable or {}).get("dynamic_file_resolver")


def _needs_attachment_clarification(state: DynamicAgentState) -> bool:
    if state.get("target_capability") != "document_qa":
        return False
    docx = _docx_attachments(state)
    if len(docx) <= 1:
        return False
    goal = str(state.get("goal") or state.get("dynamic_goal") or "").lower()
    explicit_markers = (
        "第一个",
        "第一份",
        "第1",
        "1号",
        "第二个",
        "第二份",
        "第2",
        "2号",
        "first",
        "second",
    )
    if any(marker in goal for marker in explicit_markers):
        return False
    for item in docx:
        name = str(item.get("file_name") or "").strip().lower()
        if name and name in goal:
            return False
    return True


def _docx_attachments(state: DynamicAgentState) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for idx, item in enumerate(list(state.get("attachment_refs") or [])):
        if isinstance(item, str) and item.strip():
            result.append(
                {
                    "ref": f"attachment:{idx}",
                    "file_public_id": item.strip(),
                    "file_name": "",
                    "position": idx + 1,
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        if _normalized_file_ext(item) != ".docx":
            continue
        result.append(
            {
                "ref": str(item.get("ref") or f"attachment:{idx}"),
                "file_public_id": str(
                    item.get("file_public_id") or item.get("public_id") or ""
                ),
                "file_name": str(item.get("file_name") or item.get("filename") or ""),
                "position": int(item.get("position") or idx + 1),
            }
        )
    return result


def _normalized_file_ext(item: dict[str, Any]) -> str:
    ext = str(item.get("file_ext") or "").strip().lower()
    if ext:
        return ext if ext.startswith(".") else f".{ext}"
    name = str(
        item.get("file_name") or item.get("filename") or item.get("original_name") or ""
    ).strip()
    suffix = Path(name).suffix.lower()
    if suffix:
        return suffix
    mime = str(item.get("mime_type") or "").strip().lower()
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return ".docx"
    return ""


def _understanding_summary(state: DynamicAgentState) -> str:
    snapshot = state.get("request_understanding_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    goal = str(
        state.get("goal")
        or state.get("dynamic_goal")
        or state.get("user_instruction")
        or snapshot.get("summary")
        or snapshot.get("reason")
        or ""
    ).strip()
    operation = str(state.get("operation") or "").strip()
    target = str(
        state.get("target_capability")
        or snapshot.get("target_capability")
        or ""
    ).strip()
    has_attachment = bool(state.get("attachment_refs"))
    if (
        target in {"test_plan_generation", "generate_test_plan", "test_plan"}
        or _mentions_any(goal, ("测试方案", "test plan"))
    ):
        if has_attachment and _mentions_any(goal, ("模板", "template")):
            return "根据上传的需求文档和测试方案模板生成测试方案。"
        if has_attachment:
            return "根据上传的需求文档生成测试方案。"
        return "根据用户要求生成测试方案。"
    if target == "document_qa":
        if _mentions_any(goal, ("分析", "analyze")) and _mentions_any(
            goal, ("总结", "summarize", "summary")
        ):
            source_label = "上传文档" if has_attachment else "当前文档"
            return f"根据{source_label}分析并总结文档内容。"
        operation_label = {
            "summarize": "总结文档内容",
            "analyze": "分析文档内容",
            "extract": "提取文档信息",
            "compare": "对比分析文档",
        }.get(operation, "回答文档相关问题")
        source_label = "上传文档" if has_attachment else "当前文档"
        return f"根据{source_label}{operation_label}。"
    if goal:
        return goal
    return "处理当前输入。"


def _mentions_any(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = text.lower()
    return any(keyword.lower() in normalized for keyword in keywords)


def _display_plan_steps(
    plan: DynamicPlan,
    *,
    include_terminal_steps: bool = True,
) -> list[dict[str, Any]]:
    steps = [step.model_dump(mode="python") for step in plan.steps]
    if not include_terminal_steps:
        return steps
    last_step_id = str(plan.steps[-1].step_id) if plan.steps else ""
    steps.extend(
        [
            {
                "step_id": "verify_goal",
                "title": "检查分析结果",
                "action_type": "verification",
                "capability_key": "goal_verification",
                "input_refs": ["observations"],
                "depends_on": [last_step_id] if last_step_id else [],
                "success_criteria": ["result_satisfies_user_goal"],
                "status": DynamicStepStatus.PENDING.value,
            },
            {
                "step_id": "synthesize_answer",
                "title": "生成最终答复",
                "action_type": "synthesis",
                "capability_key": "final_answer_synthesis",
                "input_refs": ["analysis_results"],
                "depends_on": ["verify_goal"],
                "success_criteria": ["final_answer_visible_to_user"],
                "status": DynamicStepStatus.PENDING.value,
            },
        ]
    )
    return steps


def _attachment_options(state: DynamicAgentState) -> list[dict[str, Any]]:
    return [
        {
            "ref": item.get("ref"),
            "file_public_id": item.get("file_public_id"),
            "file_name": item.get("file_name"),
            "position": item.get("position"),
        }
        for item in _docx_attachments(state)
    ]


async def _emit_dynamic_event(
    state: DynamicAgentState,
    config: Any,
    *,
    event_type: str,
    title: str,
    content: str,
    payload: dict[str, Any] | None = None,
    node_name: str,
) -> None:
    ctx = _runtime_context(config)
    sink = getattr(ctx, "event_sink", None) if ctx is not None else None
    if sink is None:
        return
    await sink.emit(
        task_id=str(state.get("task_id") or state.get("task_public_id") or ""),
        graph_run_id=str(state.get("graph_run_id") or f"run-{state.get('task_id') or ''}"),
        node_name=node_name,
        event_type=event_type,
        title=title,
        content=content,
        payload=payload or {},
    )


async def _emit_dynamic_public_update(
    state: DynamicAgentState,
    config: Any,
    *,
    title: str,
    headline: str,
    summary: str,
    impact: str,
    next_action: str,
    step_id: str,
    step_title: str,
    step_index: int,
    node_name: str,
) -> None:
    public_update, narrative_source = await _build_step_public_update(
        state,
        config,
        step_title=step_title,
        headline=headline,
        summary=summary,
        impact=impact,
        next_action=next_action,
    )
    await _emit_dynamic_event(
        state,
        config,
        event_type="agent_observation_update",
        title=title,
        content=summary,
        payload={
            "agent_name": "TestAgent",
            "decision_id": f"dynamic-step:{step_id}",
            "step_id": step_id,
            "step_title": step_title,
            "step_index": step_index,
            "action": step_title,
            "narrative_source": narrative_source,
            "public_update": public_update,
        },
        node_name=node_name,
    )


async def _build_step_public_update(
    state: DynamicAgentState,
    config: Any,
    *,
    step_title: str,
    headline: str,
    summary: str,
    impact: str,
    next_action: str,
) -> tuple[dict[str, Any], str]:
    fallback = {
        "headline": headline,
        "summary": summary,
        "impact": impact,
        "nextAction": next_action,
        "next_action": next_action,
        "details": [],
        "narrative_text": f"{summary}\n\n{next_action}",
        "source": "template",
    }
    ctx = _runtime_context(config)
    llm_client = getattr(ctx, "llm_client", None) if ctx is not None else None
    if llm_client is None or not hasattr(llm_client, "generate_with_system"):
        logger.warning(
            "dynamic step narrative fallback | reason=llm_unavailable | task=%s | step=%s",
            state.get("task_id") or state.get("task_public_id"),
            step_title,
        )
        return fallback, "deterministic"

    system_prompt = (
        "你是 TestAgent 的测试工程师，正在向用户同步一个非工具执行步骤的进展。\n"
        "请只基于给定事实写一段自然、克制、具体的中文说明，不要写成“影响/下一步”的固定字段模板，"
        "不要说“作为AI”，不要编造数字、文件名或内部状态。\n"
        "输出必须且只能包含 <NARRATIVE>...</NARRATIVE>，内容 1 到 2 个自然段。"
    )
    user_content = json.dumps(
        {
            "step_title": step_title,
            "task_goal": str(state.get("goal") or state.get("dynamic_goal") or ""),
            "operation": str(state.get("operation") or ""),
            "target_capability": str(state.get("target_capability") or ""),
            "known_analysis_summaries": _bounded_list(state.get("analysis_results")),
            "observations": _bounded_list(state.get("observations")),
            "fallback_meaning": {
                "summary": summary,
                "impact": impact,
                "next_action": next_action,
            },
        },
        ensure_ascii=False,
        default=str,
    )[:6000]
    try:
        # Dynamic-agent progress text is non-critical.  Until its dedicated
        # Context Engine profile is wired, use the audited deterministic form
        # instead of constructing a raw provider prompt.
        return fallback, "deterministic"
        narrative_text = _extract_narrative_text(raw)
        if not narrative_text:
            raise ValueError("missing_narrative_tag")
        parsed = normalize_public_update(
            {
                "headline": headline,
                "summary": narrative_text[:200],
                "impact": "",
                "next_action": "",
                "details": [],
                "narrative_text": narrative_text,
                "source": "llm",
            }
        )
        if parsed is None:
            raise ValueError("invalid_public_update")
        update = parsed.model_dump(mode="json")
        update["source"] = "llm"
        return update, "llm"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dynamic step narrative fallback | reason=llm_failed | task=%s | step=%s | err=%s",
            state.get("task_id") or state.get("task_public_id"),
            step_title,
            exc,
        )
        return fallback, "deterministic"


def _extract_narrative_text(raw: Any) -> str:
    text = str(raw or "").strip()
    match = re.search(r"<NARRATIVE>\s*(.*?)\s*</NARRATIVE>", text, re.S | re.I)
    if match:
        return match.group(1).strip()[:800]
    return ""


def _bounded_list(value: Any, limit: int = 5) -> list[Any]:
    if isinstance(value, dict):
        return list(value.values())[:limit]
    if isinstance(value, list):
        return value[:limit]
    return []


__all__ = ["NODE_INITIALIZE", "build_dynamic_agent_v1_graph", "initialize"]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Dynamic Agent v1 graph skeleton - Phase-1):
#
#   Phase-1 提供一个固定的、可 checkpointable 的最小图骨架:
#     nodes: planner / executor / verifier 的占位节点(暂时都返回 state 透传)
#     edges: START → planner → executor → verifier → END
#
#   Phase 2.8C 后续 phase 在不改变 test_plan graph 拓扑的前提下,
#     给本图逐渐加 LLM 真实调用 + 工具节点 + 路由策略。
#
# 关键约束(供开发者速查):
#   - 这是独立的 Dynamic Agent 子图,与 test_plan 图完全解耦;
#   - graph name 注册到 graph_registry('dynamic_agent_v1'),仅在
#     dynamic_agent_api_enabled=True 时被 dispatch;
#   - planner/executor/verifier 都是 sync 占位,real LLM 调用在后续 phase;
#   - checkpoint 由 checkpointer_factory 统一管理(不直接裸用 MemorySaver)。
