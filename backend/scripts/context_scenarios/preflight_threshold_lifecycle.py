"""Retained Hard/Absolute Context Preflight diagnostic.

This is a deliberately test-only, *scaled-window* harness.  It exercises the
same ``ContextPreflightService`` and production ``ConversationCompactor`` as
normal chat, but with a 20K-token model window.  Scaling the window makes the
Hard/Absolute branches practical to inspect with substantive release-governance
material, instead of sending a meaningless 180K-token user message.

The source corpus is five distinct operational evidence packages (orders,
inventory, payments, fulfilment and release operations), plus normal system
rules, a current goal and concise user preferences.  A retained conversation,
payload-first recovery manifest, before/after Markdown snapshots and a durable
checkpoint are created for each branch.  ``--resume`` always continues the
same specimen; it never recreates prior data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = SCRIPTS_ROOT.parent
for path in (SCRIPTS_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from phase2_lct_runtime import LctRun, cli_failure, repository_root
from phase2_live_api_e2e import ApiClient, _data

from app.agent_runtime.production_runtime_context_factory import (
    _SessionScopedSettingsService,
    _build_llm_client_for_user,
)
from app.context_engine.compression.conversation_compactor import ConversationCompactor
from app.context_engine.compression.preflight_service import ContextPreflightService
from app.context_engine.models.context import ContextItem, ContextPlan, ContextRequest, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.selection import SelectedContextSet
from app.context_engine.planning.budget_calculator import ContextBudgetCalculator
from app.context_engine.profiles.registry import BASE_POLICY_LIGHT_DIALOG
from app.db.session import AsyncSessionLocal, async_engine
from app.models.conversation import Conversation
from app.common.token_estimator import estimate_tokens
from sqlalchemy import select

SCENARIO = "CTX-PREFLIGHT-THRESHOLDS"
STATE = "CTX-PREFLIGHT-THRESHOLDS-retained-state.json"
WINDOW = 20_000
POLICY = BASE_POLICY_LIGHT_DIALOG
DOMAINS = (
    ("订单", "订单创建、支付确认、分仓、出库、签收与取消", "状态权威写入方、取消窗口与双终态"),
    ("库存", "预占、确认、释放、超卖保护与仓库切换", "SKU+仓库差异、并发切换与可售库存"),
    ("支付", "回调、重复通知、退款、拒付与日终对账", "幂等键、金额边界与冲正归属"),
    ("履约", "拣货、复核、面单、揽收、轨迹与异常件", "交接凭据、承运回执与异常归属"),
    ("发布", "灰度名单、观察窗、停止条件、回滚命令与变更单", "放量范围、回滚版本与变更单串联"),
)


def _state_path() -> Path:
    return repository_root() / "test-results" / "phase2-resource-pack" / STATE


def _read_state() -> dict[str, Any]:
    value = json.loads(_state_path().read_text(encoding="utf-8"))
    if value.get("scenario") != SCENARIO:
        raise RuntimeError("retained checkpoint is not owned by this scenario")
    return value


def _write_state(value: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _require(value: Any, key: str) -> str:
    found = value.get(key) if isinstance(value, dict) else None
    if not found:
        raise RuntimeError(f"response missing {key}")
    return str(found)


def _budget() -> Any:
    return ContextBudgetCalculator().calculate(model_context_window=WINDOW, policy=POLICY)


def _evidence_text(domain: tuple[str, str, str], target_tokens: int) -> str:
    """Produce a dense but meaningful operational evidence register, not filler."""
    name, scope, concern = domain
    rows: list[str] = [
        f"# {name}发布治理证据包\n",
        f"适用范围：{scope}。本包用于发布前风险评审、联调取证和回滚决策；所有结论均需区分事实、推断与待确认。\n",
    ]
    index = 1
    while estimate_tokens("\n".join(rows)) < target_tokens:
        region = ("华东", "华南", "欧洲", "北美", "东南亚")[index % 5]
        rows.append(
            f"{index:03d}. {region}{name}演练批次 RG-{index:04d}：核对{scope}的{concern}。"
            f"责任角色为{('业务负责人','SRE值班长','数据平台主管','合规负责人')[index % 4]}；"
            f"证据要求为 traceId=RG-{index:04d}、变更单 CHG-{260000 + index}、操作时间窗、前后状态、审批与回滚记录。"
            f"风险判定：若权威来源与现场记录不一致，冻结扩量并按既有变更单人工收口；"
            f"待确认项：阈值、值班响应时限与最终放行人不得由本记录自行补全。"
        )
        index += 1
    return "\n".join(rows)


def _selected(stage: str, budget: Any) -> SelectedContextSet:
    # Keep the source content genuinely close to the tested waterline.
    target = int(budget.hard_compact_threshold + 400) if stage == "hard" else int(budget.absolute_threshold + 400)
    fixed = [
        (ContextKind.SYSTEM_RULES, SourceType.SYSTEM, "系统规则：仅依据可追溯证据；不得将待确认项写成已批准决定；P0未关闭不得全量发布。"),
        (ContextKind.CALL_CONTRACT, SourceType.SYSTEM, "调用合同：输出必须列出事实、风险边界、证据、责任人和下一步；保留精确编号与时间窗。"),
        (ContextKind.CURRENT_GOAL, SourceType.CONVERSATION, f"执行 {stage} 阈值的跨境履约发布治理压缩审计，验证摘要与恢复载荷可追溯。"),
        (ContextKind.MEMORY, SourceType.USER_MEMORY, "用户偏好：发布评审先给结论，再给证据、责任人和待确认项；不创建任务或文件。"),
        (ContextKind.MEMORY, SourceType.USER_MEMORY, "用户偏好：任何回滚建议必须保留变更单号、执行人、时间窗和恢复核对证据。"),
    ]
    fixed_tokens = sum(estimate_tokens(text) for _, _, text in fixed)
    per_doc = max(900, (target - fixed_tokens) // len(DOMAINS) + 60)
    items: list[ContextItem] = []
    for index, (kind, source_type, content) in enumerate(fixed, start=1):
        items.append(ContextItem(
            item_id=f"{stage}-fixed-{index}", kind=kind, source_type=source_type,
            source_ref=f"diagnostic:{stage}:fixed:{index}", content=content,
            title=kind.value, authority=100 if kind in {ContextKind.SYSTEM_RULES, ContextKind.CALL_CONTRACT} else 80,
            priority=100 if kind in {ContextKind.SYSTEM_RULES, ContextKind.CALL_CONTRACT, ContextKind.CURRENT_GOAL} else 30,
            estimated_tokens=estimate_tokens(content), trust=ContextTrust.TRUSTED_INSTRUCTION,
            metadata={"section_id": kind.value},
        ))
    for index, domain in enumerate(DOMAINS, start=1):
        content = _evidence_text(domain, per_doc)
        items.append(ContextItem(
            item_id=f"{stage}-evidence-{index}", kind=ContextKind.EVIDENCE,
            source_type=SourceType.PARSED_DOCUMENT, source_ref=f"diagnostic:{stage}:governance:{index}",
            title=f"{domain[0]}发布治理证据包", content=content, authority=90, priority=50,
            relevance_score=1.0, estimated_tokens=estimate_tokens(content), trust=ContextTrust.BUSINESS_EVIDENCE,
            metadata={"section_id": "evidence"},
        ))
    total = sum(item.estimated_tokens for item in items)
    return SelectedContextSet(
        included=items, total_estimated_tokens=total,
        section_stats={kind.value: {"included_count": sum(item.kind == kind for item in items), "estimated_tokens": sum(item.estimated_tokens for item in items if item.kind == kind)} for kind in ContextKind},
    )


def _plan(budget: Any) -> ContextPlan:
    sections = {
        kind.value: SectionPlan(kind=kind, required=kind in {ContextKind.SYSTEM_RULES, ContextKind.CALL_CONTRACT, ContextKind.CURRENT_GOAL}, budget_tokens=0)
        for kind in ContextKind
    }
    return ContextPlan(
        profile_key="diagnostic.preflight.scaled-window.v1", profile_version="v1",
        model_context_window=WINDOW, input_budget=budget.target_input, output_reserve=budget.output_reserve,
        runtime_reserve=budget.runtime_reserve, safety_margin=budget.safety_margin,
        soft_threshold=budget.soft_threshold, hard_compact_threshold=budget.hard_compact_threshold,
        absolute_threshold=budget.absolute_threshold, section_plans=sections,
        compression_policy="diagnostic_conversation_compaction",
    )


def _render_snapshot(path: Path, *, stage: str, phase: str, selected: SelectedContextSet, result: Any | None = None) -> None:
    lines = [f"# {SCENARIO} {stage.upper()} {phase}", "", f"- 测试窗口：`{WINDOW}` tokens", f"- 选中总量：`{selected.total_estimated_tokens}` tokens", "", "## 选中内容"]
    for item in selected.included:
        lines.extend([f"### {item.title or item.item_id}", "", f"- kind：`{item.kind}`", f"- 来源：`{item.source_ref}`", f"- 估算 tokens：`{item.estimated_tokens}`", "", "~~~text", item.content, "~~~", ""])
    if result is not None:
        lines.extend(["## Preflight 结果", "", "```json", json.dumps({"status": str(result.status), "action": str(result.action), "tokens_before": result.tokens_before, "tokens_after": result.tokens_after, "target_tokens": result.target_tokens, "summary_refs": [ref.to_state_dict() for ref in result.compacted_summary_refs]}, ensure_ascii=False, indent=2), "```"])
    path.write_text("\n".join(lines), encoding="utf-8")


async def _resolve_identity(conversation_public_id: str) -> tuple[int, int]:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(Conversation.id, Conversation.user_id).where(Conversation.public_id == conversation_public_id))).one_or_none()
        if row is None:
            raise RuntimeError("diagnostic conversation not found")
        return int(row[0]), int(row[1])


async def _execute(stage: str, conversation_public_id: str, base: Path) -> dict[str, Any]:
    base.mkdir(parents=True, exist_ok=True)
    budget = _budget()
    selected = _selected(stage, budget)
    if stage == "hard":
        if not budget.hard_compact_threshold <= selected.total_estimated_tokens < budget.absolute_threshold:
            raise RuntimeError("hard corpus did not land inside hard band")
    elif selected.total_estimated_tokens < budget.absolute_threshold:
        raise RuntimeError("absolute corpus did not reach absolute band")
    conversation_id, user_id = await _resolve_identity(conversation_public_id)
    settings = _SessionScopedSettingsService(AsyncSessionLocal, user_id)
    llm_client = await _build_llm_client_for_user(settings, user_id)
    if llm_client is None:
        raise RuntimeError("no configured LLM client for diagnostic user")
    runtime = SimpleNamespace(user_internal_id=user_id, conversation_internal_id=conversation_id, llm_client=llm_client)
    request = ContextRequest(user_id=str(user_id), conversation_id=str(conversation_id), conversation_public_id=conversation_public_id, call_site=f"diagnostic.preflight.{stage}", current_user_message=f"请压缩 {stage} 阈值下的发布治理资料，并保留全部事实、风险、证据和待确认项。", workspace_key=f"diagnostic:{conversation_public_id}")
    before = base / f"{stage}-before.md"
    after = base / f"{stage}-after.md"
    _render_snapshot(before, stage=stage, phase="before", selected=selected)
    preflight = ContextPreflightService(conversation_compactor=ConversationCompactor(session_factory=AsyncSessionLocal), enabled=True)
    result = await preflight.run(request=request, plan=_plan(budget), selected=selected, runtime_context=runtime)
    if str(result.action.value) != "compact":
        raise RuntimeError(f"{stage} branch did not compact: status={result.status} action={result.action}")
    _render_snapshot(after, stage=stage, phase="after", selected=result.selected, result=result)
    return {"stage": stage, "before": str(before), "after": str(after), "budget": {"hard": budget.hard_compact_threshold, "absolute": budget.absolute_threshold}, "before_tokens": selected.total_estimated_tokens, "after_tokens": result.tokens_after, "summary_refs": [ref.to_state_dict() for ref in result.compacted_summary_refs]}


async def _execute_and_dispose(stage: str, conversation_public_id: str, base: Path) -> dict[str, Any]:
    try:
        return await _execute(stage, conversation_public_id, base)
    finally:
        await async_engine.dispose()


def _template(path: Path, evidence: dict[str, Any]) -> None:
    path.write_text(f"# {SCENARIO} {evidence['stage'].upper()} 人工分析\n\n- 前快照：`{evidence['before']}`\n- 后快照：`{evidence['after']}`\n- 触发前 / 后：`{evidence['before_tokens']}` / `{evidence['after_tokens']}` tokens\n- 摘要引用：`{json.dumps(evidence['summary_refs'], ensure_ascii=False)}`\n\n## 核对结论（由审阅者填写）\n\n- 业务事实、数字、责任人、风险与证据是否仍可追溯：\n- 是否只压缩了可压缩资料，系统规则/调用合同/当前目标仍保留：\n- 是否允许继续下一阶段：\n", encoding="utf-8")


def _create_state(args: argparse.Namespace, run: LctRun) -> dict[str, Any]:
    username, password = os.getenv("PHASE2_E2E_USERNAME", ""), os.getenv("PHASE2_E2E_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("PHASE2_E2E_USERNAME and PHASE2_E2E_PASSWORD are required")
    client = ApiClient(args.base_url, timeout_seconds=args.request_timeout_seconds)
    client.login(username, password)
    marker = _stamp()
    project = _data(client.request("POST", "/projects", {"name": f"{SCENARIO} {marker}", "description": "保留的 Hard/Absolute 缩放窗口诊断会话。", "memoryMode": "project_memory"}))
    project_id = _require(project, "id")
    conversation = _data(client.request("POST", f"/projects/{project_id}/conversations", {"title": f"{SCENARIO} 发布治理 {marker}"}))
    conversation_id = _require(conversation, "id")
    _data(client.request("PUT", f"/projects/{project_id}/instructions", {"instructions": "本会话用于受控上下文压缩诊断。所有结论必须保留事实、风险、证据、责任人与待确认项；不得将测试材料当成生产批准。"}))
    base = repository_root() / "test-results" / "phase2-resource-pack" / "preflight-threshold-lifecycle" / conversation_id
    state = {"scenario": SCENARIO, "project_id": project_id, "conversation_id": conversation_id, "base_dir": str(base), "status": "hard", "reports": []}
    _write_state(state)
    run.observe(run.step("create retained diagnostic conversation", component="Project + Conversation APIs", expected="a visible retained conversation exists for threshold audit"), True, actual=json.dumps({"project_id": project_id, "conversation_id": conversation_id}))
    return state


def _append(path: Path, analysis: Path) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n\n---\n\n" + analysis.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000/api")
    parser.add_argument("--request-timeout-seconds", type=int, default=240)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--analysis-file")
    parser.add_argument("--retry-absolute", action="store_true", help="rerun Absolute after correcting a diagnosed compaction defect")
    args = parser.parse_args()
    run = LctRun(lct=SCENARIO, repo_root=repository_root(), execution_path="RETAINED_SCALED_WINDOW_PREFLIGHT + REAL_COMPACTOR + HUMAN_CHECKPOINT")
    try:
        state = _read_state() if args.resume else _create_state(args, run)
        if args.resume:
            if args.retry_absolute:
                if state["status"] != "absolute_review":
                    raise RuntimeError("--retry-absolute requires the retained absolute_review checkpoint")
                state["status"] = "absolute_retry"; _write_state(state)
            elif state["status"] in {"hard_review", "absolute_review", "absolute_retry_review"}:
                if not args.analysis_file:
                    raise RuntimeError("resume requires a completed --analysis-file at a review checkpoint")
                _append(Path(state["base_dir"]) / "threshold-analysis.md", Path(args.analysis_file))
                if state["status"] in {"absolute_review", "absolute_retry_review"}:
                    state["status"] = "completed"; _write_state(state)
                    return run.finish(lct_verdict="LCT_PASS", classification="scaled-window hard/absolute compaction diagnostic")
                state["status"] = "absolute"; _write_state(state)
            elif state["status"] not in {"hard", "absolute", "absolute_retry"}:
                raise RuntimeError(f"cannot resume retained scenario from status={state['status']}")
        stage = str(state["status"])
        evidence = asyncio.run(_execute_and_dispose(stage, str(state["conversation_id"]), Path(state["base_dir"])))
        analysis = Path(state["base_dir"]) / f"{stage}-analysis-template.md"
        _template(analysis, evidence)
        state.update({"status": f"{stage}_review", "last_evidence": evidence, "analysis_template": str(analysis)})
        state["reports"].append(evidence)
        _write_state(state)
        run.observe(run.step(f"durably compact {stage} threshold", component="ContextPreflightService + ConversationCompactor", expected=f"{stage} branch compacts substantive governance sources and persists a recovery-backed summary"), True, actual=json.dumps(evidence, ensure_ascii=False))
        run.notes.append("PAUSED_FOR_HUMAN_COMPARISON: inspect the before/after exports, fill the analysis template, then resume.")
        return run.finish(lct_verdict="LCT_PARTIAL_PASS", classification="scaled-window hard/absolute compaction diagnostic")
    except Exception as exc:
        return cli_failure(run, exc, classification="scaled-window hard/absolute compaction diagnostic")
if __name__ == "__main__":
    raise SystemExit(main())
