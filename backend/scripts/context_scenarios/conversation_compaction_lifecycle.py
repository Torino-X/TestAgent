"""Retained, pauseable normal-chat compaction lifecycle scenario.

This scenario exercises the production 60% conversation-retention transitions
with a project team’s ordinary release-readiness discussion.  It intentionally
does *not* use one giant prompt or repeated filler.  Each turn records a new,
chronological decision, evidence request, defect finding, or handoff item.

The runner stops after every observed durable compaction.  It exports the
canonical five-category working set immediately before and after the transition
and requires an operator/Codex review Markdown file before ``--resume`` may
start the next round.  Resources are deliberately retained for UI inspection.

Hard/Absolute preflight guards are reported, but are not claimed as covered by
this normal short-chat scenario: the production ``chat.reply`` profile limits
Memory to 1,000 and project instructions to 2,000 tokens, so inflating those
real sources cannot honestly force those guards.  Reaching them would require
an oversized current prompt or a product-profile redesign.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = SCRIPTS_ROOT.parent
for _path in (SCRIPTS_ROOT, BACKEND_ROOT, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from phase2_lct_runtime import LctRun, cli_failure, repository_root
from phase2_live_api_e2e import ApiClient, _data


SCENARIO = "CTX-COMPACTION-LIFECYCLE-V2"
WINDOW_TOKENS = 200_000
PROACTIVE_PERCENT = 60.0
PROACTIVE_TOKENS = int(WINDOW_TOKENS * PROACTIVE_PERCENT / 100)
STATE_NAME = "CTX-COMPACTION-LIFECYCLE-V2-retained-state.json"
EXPORTER = SCRIPTS_ROOT / "export_conversation_context_working_set.py"

# A scenario run must observe both production retention tiers in order.  The
# policy key is read from the immutable compaction audit rather than inferred
# from the visible usage drop, which may be bounded by a protected raw tail.
RETENTION_STAGES = (
    ("light", "conversation-retention.light.v2", 20, 55.0),
    ("deep", "conversation-retention.deep.v2", 12, 52.0),
)

WORKSTREAMS = (
    ("订单", "订单创建、支付确认、分仓、出库、签收与取消的状态证据"),
    ("库存", "预占、确认、释放、超卖保护和仓库切换的差异处理"),
    ("支付", "支付回调、重复通知、退款、拒付和日终对账的审计链"),
    ("履约", "拣货、复核、面单、揽收、轨迹与异常件的交接证据"),
    ("风控", "规则命中、人工复核、超时升级和申诉回放的留痕"),
    ("隐私", "最小权限、字段脱敏、导出审批和跨境数据访问边界"),
    ("可观测性", "traceId、业务指标、告警路由、值班确认和故障复盘"),
    ("发布", "灰度名单、观察窗、停止条件、回滚命令和变更单记录"),
    ("数据迁移", "双写校验、差异清单、补数、切换和回滚后的核对"),
    ("运营", "客服口径、人工兜底、SOP、升级路径和培训交接"),
)
ROLES = ("测试负责人", "产品负责人", "研发负责人", "SRE 值班长", "数据平台主管", "区域运营负责人")
PHASES = ("基线澄清", "联调跟踪", "风险复核", "上线演练", "回归验收", "交接复盘")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _state_path() -> Path:
    return repository_root() / "test-results" / "phase2-resource-pack" / STATE_NAME


def _write_state(value: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("no readable retained lifecycle checkpoint; start without --resume") from exc
    if not isinstance(value, dict) or value.get("scenario") != SCENARIO:
        raise RuntimeError("retained lifecycle checkpoint is not owned by this scenario")
    return value


def _require_id(value: Any, *, key: str, operation: str) -> str:
    result = value.get(key) if isinstance(value, dict) else None
    if not result:
        raise RuntimeError(f"{operation} response did not contain {key}")
    return str(result)


def _create_memory(client: ApiClient, *, title: str, content: str, key: str) -> str:
    value = _data(client.request("POST", "/context/memory", {
        "scope_type": "user", "memory_type": "preference", "title": title,
        "content": content, "dedupe_key": key,
    }))
    memory_id = _require_id(value, key="memory_public_id", operation="create memory")
    _data(client.request("POST", f"/context/memory/{urllib.parse.quote(memory_id)}/activate"))
    return memory_id


def _seed_meaningful_context(client: ApiClient, *, marker: str, project_id: str) -> dict[str, list[str]]:
    """Create real, independently useful policy sources; no filler is used."""
    # Project instructions are the normal user-facing system-instruction path.
    # Do not depend on the separate workspace-instruction administration API.
    memory_ids = [
        _create_memory(client, title="测试方案篇幅偏好（旧）", content="生成测试方案时，每个章节至少达到100个字。", key="test_plan_chapter_min_words"),
        _create_memory(client, title="测试方案篇幅偏好（现行）", content="生成测试方案时，每个章节至少达到500个字，替代此前100字要求。", key="test_plan_chapter_min_words"),
    ]
    for index, (name, concern) in enumerate(WORKSTREAMS[:8], start=1):
        memory_ids.append(_create_memory(
            client,
            title=f"发布沟通偏好 {index}",
            content=f"用户偏好：讨论{name}时要先给当前结论，再列出{concern}对应的证据、责任人和待确认项。",
            key=f"release-preference-{marker}-{index:02d}",
        ))
    return {"instruction_ids": [], "memory_ids": memory_ids}


def _turn_prompt(*, ordinal: int, marker: str) -> str:
    workstream, concern = WORKSTREAMS[(ordinal - 1) % len(WORKSTREAMS)]
    role = ROLES[(ordinal - 1) % len(ROLES)]
    phase = PHASES[((ordinal - 1) // len(WORKSTREAMS)) % len(PHASES)]
    return (
        f"[{SCENARIO}:{marker}:R{ordinal:03d}] 我是{role}，现在处于{phase}。"
        f"这是一轮普通工作讨论。请围绕{workstream}的“{concern}”，用三条简短要点说明："
        "1. 下一次评审最需要澄清的业务判断；2. 应提前防范的一个失败边界；"
        "3. 应由哪个岗位确认。请直接基于当前讨论回答，无需向我追问或进入澄清流程，"
        "不生成任务、测试方案或其他产物。"
    )


def _send_chat(client: ApiClient, conversation_id: str, content: str) -> dict[str, Any]:
    value = _data(client.request("POST", f"/conversations/{urllib.parse.quote(conversation_id)}/messages", {
        "content": content, "attached_file_ids": [], "knowledge_mode_snapshot": "AUTO",
    }))
    return value if isinstance(value, dict) else {}


def _normal_chat_route_error(response: dict[str, Any], *, ordinal: int) -> str | None:
    """Return a diagnostic when a persisted turn did not exercise chat preflight."""
    reply = response.get("agent_reply") if isinstance(response, dict) else None
    payload = reply.get("payload") if isinstance(reply, dict) else None
    intent = str((payload or {}).get("intent") or response.get("intent") or "")
    bridge = (payload or {}).get("chat_context_engine")
    outcome = bridge.get("outcome") if isinstance(bridge, dict) else None
    if (
        str(response.get("route") or "") != "chat_reply"
        or intent != "general_chat"
        or outcome != "completed"
    ):
        return (
            f"turn {ordinal} did not exercise normal Context Engine chat: "
            f"route={response.get('route')!r}, intent={intent!r}, bridge={outcome!r}"
        )
    return None


def _record_route_fallback(state: dict[str, Any], *, ordinal: int, error: str) -> bool:
    """Persist one non-chat route and keep the next retry semantically new.

    ``POST /messages`` persists the user message even when intent routing falls
    back to clarify.  Re-sending the same ordinal on resume would therefore
    duplicate a real conversation entry.  It is not an eligible chat turn, so
    retain the evidence and advance to the next normal business prompt.  A
    repeated fallback remains a hard test failure rather than being hidden.
    """
    fallbacks = list(state.get("route_fallbacks") or [])
    fallbacks.append({"ordinal": ordinal, "error": error})
    state["route_fallbacks"] = fallbacks[-20:]
    consecutive = int(state.get("consecutive_route_fallbacks", 0)) + 1
    state["consecutive_route_fallbacks"] = consecutive
    state["next_turn"] = ordinal + 1
    _write_state(state)
    return consecutive >= 3


def _usage(client: ApiClient, conversation_id: str) -> dict[str, Any]:
    value = _data(client.request("GET", f"/conversations/{urllib.parse.quote(conversation_id)}/context-usage"))
    return value if isinstance(value, dict) else {}


def _compactions(client: ApiClient, conversation_id: str) -> list[dict[str, Any]]:
    value = _data(client.request("GET", f"/context/audit/compaction?conversation_id={urllib.parse.quote(conversation_id)}&limit=100"))
    return [row for row in (value.get("items") if isinstance(value, dict) else []) or [] if isinstance(row, dict)]


def _completed_conversation_compactions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("status") == "completed" and row.get("compaction_type") == "conversation"]


def _export(conversation_id: str, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, str(EXPORTER), conversation_id, "--output", str(output)],
        cwd=BACKEND_ROOT, text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"working-set export failed: {completed.stderr[-600:]}")
    if not output.exists():
        raise RuntimeError("working-set exporter exited without writing its Markdown output")
    return str(output)


def _snapshot_stats(usage: dict[str, Any], compactions: list[dict[str, Any]]) -> dict[str, Any]:
    model = usage.get("model") if isinstance(usage.get("model"), dict) else {}
    visible = usage.get("usage") if isinstance(usage.get("usage"), dict) else {}
    return {
        "used_tokens": visible.get("used_tokens"), "percent": visible.get("percent"),
        "breakdown": usage.get("breakdown") or {},
        "window_tokens": model.get("context_window_tokens"),
        "conversation_compactions": len(_completed_conversation_compactions(compactions)),
    }


def _expected_stage(state: dict[str, Any]) -> tuple[str, str, int, float]:
    index = int(state.get("stage_index", 0))
    if not 0 <= index < len(RETENTION_STAGES):
        raise RuntimeError(f"invalid retention stage index: {index}")
    return RETENTION_STAGES[index]


def _write_analysis_template(path: Path, *, state: dict[str, Any]) -> str:
    before = state["before_export"]
    after = state["after_export"]
    name, policy_key, retained_turns, target_percent = _expected_stage(state)
    text = f"""# {SCENARIO} 第 {state['round']} 轮 {name} 压缩人工分析\n\n## 快照\n\n- 压缩前：`{before}`\n- 压缩后：`{after}`\n- 预期策略：`{policy_key}`\n- 预期原文尾部：最近 `{retained_turns}` 个完整用户轮次\n- 目标回落：约 `{target_percent:.0f}%`（受不可压缩尾部约束，需以实际审计解释）\n- 触发前统计：`{json.dumps(state['before_stats'], ensure_ascii=False)}`\n- 触发后统计：`{json.dumps(state['after_stats'], ensure_ascii=False)}`\n- 实际压缩审计：`{json.dumps(state.get('observed_compaction') or {}, ensure_ascii=False)}`\n\n## 必须核对\n\n1. 审计策略是否为预期的 `{policy_key}`，且当前摘要覆盖范围连续包含上一次摘要的结论。\n2. 最近 `{retained_turns}` 个完整用户轮次是否仍以原文保留。\n3. 已确认决定、需求变更、责任人、时间窗口、风险与证据要求是否仍可追溯。\n4. 是否只折叠较早对话，且项目资料、Memory、系统指令、任务上下文没有非预期丢失。\n5. 是否出现相互冲突的用户偏好；本场景中“每章节至少500字”应替代旧的“100字”。\n\n## 结论（由审阅者填写）\n\n- 保留效果：\n- 丢失/错误：\n- 是否允许继续下一轮：\n"""
    path.write_text(text, encoding="utf-8")
    return str(path)


def _append_analysis(master: Path, analysis: Path, *, state: dict[str, Any]) -> None:
    text = analysis.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError("--analysis-file is empty; inspect the two exports before resuming")
    master.parent.mkdir(parents=True, exist_ok=True)
    with master.open("a", encoding="utf-8") as stream:
        stream.write(f"\n\n---\n\n# 第 {state['round']} 轮人工分析\n\n{text}\n")


def _initial_state(args: argparse.Namespace, run: LctRun) -> dict[str, Any]:
    username, password = os.environ.get("PHASE2_E2E_USERNAME", ""), os.environ.get("PHASE2_E2E_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("PHASE2_E2E_USERNAME and PHASE2_E2E_PASSWORD are required")
    marker = _stamp()
    client = ApiClient(args.base_url, timeout_seconds=args.request_timeout_seconds)
    client.login(username, password)
    project = _data(client.request("POST", "/projects", {
        "name": f"{SCENARIO} 发布评审 {marker}", "description": "保留的真实短对话压缩生命周期样本。", "memoryMode": "project_memory",
    }))
    project_id = _require_id(project, key="id", operation="create project")
    conversation = _data(client.request("POST", f"/projects/{urllib.parse.quote(project_id)}/conversations", {"title": f"{SCENARIO} 发布指挥室 {marker}"}))
    conversation_id = _require_id(conversation, key="id", operation="create conversation")
    project_instruction = "这是跨境履约发布评审会话。所有结论必须区分事实、推断和待确认项；P0未关闭不得全量发布；每项结论都要写明责任人、证据和下一步。\n\n" + "\n".join(
        f"- {name}：{concern}；必须记录可复现步骤、证据、责任人和回滚或人工兜底条件。"
        for name, concern in WORKSTREAMS
    )
    _data(client.request("PUT", f"/projects/{urllib.parse.quote(project_id)}/instructions", {
        "instructions": project_instruction,
    }))
    base = repository_root() / "test-results" / "phase2-resource-pack" / "compaction-lifecycle" / conversation_id
    state = {
        "scenario": SCENARIO, "marker": marker, "project_id": project_id, "conversation_id": conversation_id,
        "instruction_ids": [], "memory_ids": [], "round": 1, "stage_index": 0,
        "next_turn": 1, "max_turns_per_round": args.max_turns_per_round, "max_rounds": args.max_rounds,
        "base_dir": str(base), "master_report": str(base / "compression-analysis.md"), "status": "initializing",
    }
    # Persist ownership before optional context-source setup. A later API
    # failure must never make a retained diagnostic conversation undiscoverable.
    _write_state(state)
    seeded = _seed_meaningful_context(client, marker=marker, project_id=project_id)
    state.update({
        "instruction_ids": seeded["instruction_ids"],
        "memory_ids": seeded["memory_ids"],
        "status": "running",
    })
    _write_state(state)
    run.observe(run.step("create retained lifecycle specimen", component="Project + Conversation APIs", expected="project, conversation, meaningful instructions and memories exist"), True, actual=json.dumps({"project_id": project_id, "conversation_id": conversation_id, "instructions": len(seeded["instruction_ids"]), "memories": len(seeded["memory_ids"])}, ensure_ascii=False))
    return state


def _run_until_checkpoint(args: argparse.Namespace, run: LctRun, state: dict[str, Any]) -> str:
    username, password = os.environ.get("PHASE2_E2E_USERNAME", ""), os.environ.get("PHASE2_E2E_PASSWORD", "")
    client = ApiClient(args.base_url, timeout_seconds=args.request_timeout_seconds)
    client.login(username, password)
    conversation_id = str(state["conversation_id"])
    baseline = _completed_conversation_compactions(_compactions(client, conversation_id))
    baseline_ids = {str(row.get("public_id") or "") for row in baseline}
    round_no = int(state["round"])
    stage_name, expected_policy, _retained_turns, _target_percent = _expected_stage(state)
    start_turn = int(state["next_turn"])
    last_usage: dict[str, Any] = {}
    for ordinal in range(start_turn, start_turn + int(state["max_turns_per_round"])):
        before_usage = _usage(client, conversation_id)
        before_rows = _compactions(client, conversation_id)
        before_stats = _snapshot_stats(before_usage, before_rows)
        # The card’s total includes all five categories, so leave a small
        # source allowance before declaring this the pre-compaction snapshot.
        if int(((before_usage.get("usage") or {}).get("used_tokens") or 0)) >= PROACTIVE_TOKENS:
            before_export = _export(conversation_id, Path(state["base_dir"]) / f"round-{round_no:02d}-before.md")
            trigger_response = _send_chat(client, conversation_id, _turn_prompt(ordinal=ordinal, marker=str(state["marker"])))
            route_error = _normal_chat_route_error(trigger_response, ordinal=ordinal)
            if route_error:
                if _record_route_fallback(state, ordinal=ordinal, error=route_error):
                    raise RuntimeError(f"three consecutive non-chat route fallbacks; latest: {route_error}")
                continue
            state["consecutive_route_fallbacks"] = 0
            deadline = time.monotonic() + args.compaction_wait_seconds
            after_rows = _compactions(client, conversation_id)
            while len(_completed_conversation_compactions(after_rows)) <= len(baseline) and time.monotonic() < deadline:
                time.sleep(2)
                after_rows = _compactions(client, conversation_id)
            after_usage = _usage(client, conversation_id)
            after_export = _export(conversation_id, Path(state["base_dir"]) / f"round-{round_no:02d}-after.md")
            newly_completed = [
                row
                for row in _completed_conversation_compactions(after_rows)
                if str(row.get("public_id") or "") not in baseline_ids
            ]
            # Audit responses are normally newest-first, but sort explicitly so
            # endpoint ordering cannot make this assertion flaky.
            newly_completed.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
            observed = newly_completed[0] if newly_completed else None
            compaction_seen = observed is not None
            policy_matches = bool(observed and observed.get("policy_key") == expected_policy)
            checkpoint_status = "awaiting_analysis" if compaction_seen else "compaction_not_observed"
            state.update({
                "status": checkpoint_status, "next_turn": ordinal + 1,
                "before_export": before_export, "after_export": after_export,
                "before_stats": before_stats, "after_stats": _snapshot_stats(after_usage, after_rows),
                "compaction_rows": after_rows,
                "expected_stage": stage_name, "expected_policy": expected_policy,
                "observed_compaction": observed,
            })
            template = _write_analysis_template(Path(state["base_dir"]) / f"round-{round_no:02d}-analysis-template.md", state=state)
            state["analysis_template"] = template
            _write_state(state)
            run.observe(run.step(f"observe durable 60% {stage_name} conversation compaction", component="Context Compaction audit", expected=f"a normal short-turn chat request records {expected_policy} and folds only the older conversation prefix"), compaction_seen and policy_matches, actual=json.dumps({"after": state["after_stats"], "observed": observed}, ensure_ascii=False), evidence={"before_export": before_export, "after_export": after_export, "analysis_template": template})
            run.notes.append("PAUSED_FOR_HUMAN_COMPARISON: fill the generated analysis template, then rerun with --resume --analysis-file <file>.")
            if not compaction_seen:
                run.notes.append("COMPACTION_NOT_OBSERVED: retained specimen is resumable after the backend trigger is repaired; no conversation history was discarded.")
                return "TEST_FAIL"
            if not policy_matches:
                state["status"] = "policy_mismatch"
                _write_state(state)
                raise RuntimeError(f"expected {expected_policy}, observed {observed.get('policy_key') if observed else None}")
            return "LCT_PARTIAL_PASS"
        response = _send_chat(client, conversation_id, _turn_prompt(ordinal=ordinal, marker=str(state["marker"])))
        route_error = _normal_chat_route_error(response, ordinal=ordinal)
        if route_error:
            if _record_route_fallback(state, ordinal=ordinal, error=route_error):
                raise RuntimeError(f"three consecutive non-chat route fallbacks; latest: {route_error}")
            continue
        state["consecutive_route_fallbacks"] = 0
        last_usage = _usage(client, conversation_id)
        state["next_turn"] = ordinal + 1
        _write_state(state)
    raise RuntimeError(f"round {round_no} did not reach the 60% conversation threshold within {state['max_turns_per_round']} meaningful turns; last_usage={_snapshot_stats(last_usage, _compactions(client, conversation_id))}")


def _resume(args: argparse.Namespace, run: LctRun, state: dict[str, Any]) -> str:
    if state.get("status") == "running":
        # The process may be intentionally interrupted between ordinary chat
        # turns. Progress is durable, so continue from ``next_turn`` without
        # inventing an analysis checkpoint that has not happened yet.
        run.notes.append("RESUMED_RUNNING_CHECKPOINT: continuing retained short-turn conversation.")
        return _run_until_checkpoint(args, run, state)
    if state.get("status") == "compaction_not_observed":
        run.notes.append("RESUMED_AFTER_TRIGGER_REPAIR: continuing the same retained conversation after an observed no-compaction checkpoint.")
        state["status"] = "running"
        _write_state(state)
        return _run_until_checkpoint(args, run, state)
    if state.get("status") == "policy_mismatch":
        raise RuntimeError(
            "the retained specimen observed an unexpected compaction policy; "
            "inspect its report and state before choosing whether to resume or create a separate specimen"
        )
    if state.get("status") != "awaiting_analysis":
        raise RuntimeError("checkpoint is not awaiting analysis; start a new scenario or inspect the retained state")
    analysis = Path(args.analysis_file).resolve()
    if not analysis.is_file():
        raise RuntimeError("--analysis-file must point to the completed Markdown review")
    _append_analysis(Path(state["master_report"]), analysis, state=state)
    round_no = int(state["round"])
    run.observe(run.step("record previous compaction review", component="operator analysis", expected="before/after exports are reviewed before more context is added"), True, actual=str(analysis))
    required_rounds = min(int(state["max_rounds"]), len(RETENTION_STAGES))
    completed_stages = int(state.get("stage_index", 0)) + 1
    if completed_stages >= required_rounds:
        state["status"] = "completed"
        _write_state(state)
        run.notes.append("NORMAL_CHAT_SCOPE_ONLY: Hard/Absolute were not forced because their production profile limits make that incompatible with this no-oversized-prompt scenario.")
        return "LCT_PASS" if required_rounds == len(RETENTION_STAGES) else "LCT_PARTIAL_PASS"
    state.update({"round": round_no + 1, "stage_index": completed_stages, "status": "running"})
    _write_state(state)
    return _run_until_checkpoint(args, run, state)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000/api")
    parser.add_argument("--request-timeout-seconds", type=int, default=240)
    parser.add_argument("--max-turns-per-round", type=int, default=600)
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--compaction-wait-seconds", type=int, default=120)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--analysis-file")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.analysis_file and not args.resume:
        raise SystemExit("--analysis-file requires --resume")
    if not 20 <= args.max_turns_per_round <= 2000 or not 1 <= args.max_rounds <= len(RETENTION_STAGES):
        raise SystemExit(f"max turns must be 20..2000 and max rounds 1..{len(RETENTION_STAGES)}")
    run = LctRun(lct=SCENARIO, repo_root=repository_root(), execution_path="LIVE_NORMAL_CHAT + RETAINED_PRE_POST_EXPORT + HUMAN_CHECKPOINT")
    try:
        state = _read_state() if args.resume else _initial_state(args, run)
        verdict = _resume(args, run, state) if args.resume else _run_until_checkpoint(args, run, state)
        return run.finish(lct_verdict=verdict, classification="real short-turn conversation compaction lifecycle")
    except Exception as exc:
        return cli_failure(run, exc, classification="real short-turn conversation compaction lifecycle")


if __name__ == "__main__":
    raise SystemExit(main())
