"""Phase 2.9B.4 — 通用 tool_narrative_barrier 节点(同步屏障)。

Tool 节点完成时写入 ``state.pending_narrative``(PendingNarrative);本节点
读取它,构造 ToolNarrativeContext,await NarrativeComposer 生成叙事,
完成后清理 pending_narrative 并按 continuation_route 路由。

同步屏障语义:
* 绝不 ``asyncio.create_task`` 后台生成 —— 本节点必须 await 完成或回退。
* 幂等键 ``{task_id}:{graph_run_id}:{tool_call_id}:{attempt}:tool_narrative:v1``,
  已有 final 时幂等跳过。
* Tool 已完成、叙事未完成时,checkpoint 记录该中间状态;服务重启可重入本节点,
  不重复执行 Tool。
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from app.agent_runtime._shared.public_narrative import AgentPublicUpdateDraft
from app.agent_runtime._shared.narrative_governance.settings_service import (
    is_tool_card_narrative_generation_enabled,
)
from app.agent_runtime.feature_flags import get_feature_flags
from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
    NODE_SEARCH_KNOWLEDGE,
)
from app.context_engine.feature_flags import is_agent_context_migration_enabled
from app.agent_runtime.narrative_composer.composer import NarrativeComposer
from app.agent_runtime.narrative_composer.context_builders import (
    get_tool_context_builder,
)
from app.agent_runtime.narrative_composer.prompts import build_task_summary_prompt
from app.agent_runtime.narrative_composer.schemas import (
    NarrativeFactConstraints,
    PendingNarrative,
    TaskSummaryNarrativeContext,
)
from app.agent_runtime.runtime_context import RuntimeContext
from app.llm.task_profiles import TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE

NODE_TOOL_NARRATIVE_BARRIER = "tool_narrative_barrier"
NODE_TASK_SUMMARY_NARRATIVE = "task_summary_narrative"

# 两个确定性 fallback —— 任何 LLM 路径失败(超时 / schema 校验不过 / repair 失败
# / MIG_NARRATIVE=true 但 bridge 不可用)时,NarrativeComposer 把这两个之一
# 作为 public_update 返回;前端看到的是 deterministic narrative(没有任何 LLM 痕迹)。
_DETERMINISTIC_TOOL_FALLBACK = AgentPublicUpdateDraft(
    headline="工具执行完成",
    summary="本次工具调用已按确定性流程完成。",
    impact="任务将继续按计划执行。",
    next_action="继续处理当前任务。",
    details=[],
)

_DETERMINISTIC_TASK_SUMMARY_FALLBACK = AgentPublicUpdateDraft(
    headline="任务已完成",
    summary="测试方案已按确定性流程生成并导出。",
    impact="产物可下载用于后续执行。",
    next_action="可下载并查看测试方案文档。",
    details=[],
)


def narrative_logical_key(
    task_id: str, graph_run_id: str, tool_call_id: str, attempt: int
) -> str:
    """narrative 幂等键 ——
    同一 (task, graph_run, tool_call, attempt) 只能生成一次叙事,避免 resume 重放
    时 barrier 重复 await LLM。
    """
    # 把多维 key 拼成单一字符串,放进 state.narrative_completed_logical_keys 列表里查。
    return f"{task_id}:{graph_run_id}:{tool_call_id}:{attempt}:tool_narrative:v1"


async def _tool_narrative_enabled(ctx: RuntimeContext) -> bool:
    # 双重开关 ——
    #   phase29b_tool_narrative_enabled: 总开关;
    #   phase29b_tool_narrative_blocking_enabled: 屏障模式(同步 vs fire-and-forget)。
    # 两个同时为 True,barrier 节点才走真实生成,否则直接放行(节约 LLM 成本)。
    flags = get_feature_flags()
    feature_enabled = (
        flags.phase29b_tool_narrative_enabled
        and flags.phase29b_tool_narrative_blocking_enabled
    )
    return feature_enabled and await is_tool_card_narrative_generation_enabled(ctx)


def _narrative_done(
    state: Dict[str, Any],
    logical_key: str,
) -> bool:
    """检查幂等键是否已记录 ——
    state.narrative_completed_logical_keys 由 barrier 在 narrative 完成后追加。
    服务重启 / checkpoint 重放可借此跳过重复生成。
    """
    done = state.get("narrative_completed_logical_keys") or []
    return logical_key in done


def _resolve_narrative_llm(
    ctx: RuntimeContext,
    *,
    call_site: str,
) -> Any:
    """按 MIG_NARRATIVE flag 解析叙事 LLM 客户端。

    * flag=false → 明确走 legacy（ctx.llm_client）
    * flag=true  → 只走 ContextInvokerBridge（ctx.context_llm_invoker）：
                   先 bind 调用上下文，再作为 llm_client 传入
                   NarrativeComposer；不可用时返回 None（调用方走确定性
                   回退），不静默回退 legacy LLMClient。
    """
    # CE-05 WP-2：任务路径经 ctx.task_flag_resolver 读 MIG_NARRATIVE（冻结 Manifest）；
    # resolver 缺失/损坏 → fail closed，不读取进程级 MIG 配置。
    resolver = getattr(ctx, "task_flag_resolver", None)
    mig_narrative = True
    if not mig_narrative:
        return None

    # MIG_NARRATIVE=true：只走 bridge，绝不回退 legacy —
    # 这是 CE-05 双轨改造的设计原则,避免一侧修复另一侧还在 fallback。
    bridge = getattr(ctx, "context_llm_invoker", None)
    if bridge is None or not getattr(bridge, "available", False):
        import logging
        logging.getLogger(__name__).warning(
            "narrative MIG_NARRATIVE=true 但 Invoker 不可用 | task_id=%s",
            getattr(ctx, "task_internal_id", None),
        )
        # 不可用 → 返回 None,barrier 走确定性 fallback,而不是悄悄启用旧 client。
        return None
    # 把 user / call_site / task 信息 bind 给 bridge,后续 NarrativeComposer
    # 通过它间接调模型,适用于 ContextEngine profile 路由等场景。
    return bridge.bind(
        user_id=getattr(ctx, "user_internal_id", 0),
        call_site=call_site,
        task_id=getattr(ctx, "task_internal_id", None),
        runtime_context=ctx,
    )


async def tool_narrative_barrier_node(
    state: Dict[str, Any], *, ctx: RuntimeContext
) -> Dict[str, Any]:
    """同步叙事屏障:await 叙事完成或确定性回退,然后释放进入下一节点。

    阻塞语义:
      - Tool 节点完成时往 state.pending_narrative 写一个 PendingNarrative;
      - barrier 节点 await NarrativeComposer,生成 / 修复 / fallback;
      - 完成后清空 pending_narrative 并按 continuation_route 路由到下一节点。
      - 整个过程必须 await,绝不能 create_task 后台跑(否则前端 SSE 流错位)。

    与 checkpoint 重放:
      - service 重启 / thread_id 恢复时,LangGraph 仍会跑到本节点;
      - 我们用 narrative_logical_key 幂等键识别"已完成的 narrative",
        跳过重复生成。
    """
    if not await _tool_narrative_enabled(ctx):
        # 功能关闭: 直接透传 continuation_route。
        # 这种情形下 barrier 退化成一个 pass-through 节点,只清 pending_narrative。
        pending = state.get("pending_narrative") or {}
        route = pending.get("continuation_route") or state.get("next_node") or NODE_SEARCH_KNOWLEDGE
        return {
            "pending_narrative": None,
            "next_node": route,
        }

    pending_raw = state.get("pending_narrative")
    if not pending_raw:
        # 无待叙事 → 默认路由(部分路径如 task_summary 也会跑到这里)。
        return {"pending_narrative": None, "next_node": state.get("next_node") or NODE_SEARCH_KNOWLEDGE}

    # Pydantic 校验: invalid 时让 LangGraph 抛错,router 会跳 fail_task。
    pending = PendingNarrative.model_validate(pending_raw)
    task_id = str(state.get("task_id") or "")
    graph_run_id = str(state.get("graph_run_id") or f"run-{ctx.task_internal_id}")
    logical_key = narrative_logical_key(
        task_id, graph_run_id, pending.source_tool_call_id, pending.tool_attempt
    )

    if _narrative_done(state, logical_key):
        # 幂等跳过(已有 final)。checkpoint 重放场景必走到这里。
        return {
            "pending_narrative": None,
            "next_node": pending.continuation_route,
        }

    # CE-04:MIG_NARRATIVE flag 路由——flag=true 只走 ContextInvokerBridge，
    # 不可用时返回 None → 确定性回退（不静默回退 legacy LLMClient）。
    llm_client = _resolve_narrative_llm(
        ctx,
        call_site="test_plan.tool_narrative",  # call_site 让 ContextEngine 可以按工具维度路由 profile
    )
    if llm_client is None:
        # 无可用 LLM 客户端（legacy 缺失或 MIG_NARRATIVE=true 但 Invoker 不可用）
        # → 确定性回退(不阻塞主图);不影响任务推进,只是 narrative 是固定文案。
        return {
            "pending_narrative": None,
            "next_node": pending.continuation_route,
        }

    # registry 查 builder,builder.build 把 Graph State 压缩成 ToolNarrativeContext。
    narrative_id = f"nar_{uuid.uuid4().hex[:12]}"
    generation_id = f"nargen_{uuid.uuid4().hex[:12]}"
    try:
        builder = get_tool_context_builder(pending.source_tool_name)
        context = builder.build(
            graph_state=dict(state),
            tool_call_id=pending.source_tool_call_id,
            source_event_id=pending.source_event_id,
            attempt=pending.tool_attempt,
            terminal_status=pending.terminal_status,
            duration_ms=pending.duration_ms,
            continuation_route=pending.continuation_route,
        )
        composer = NarrativeComposer(
            llm_client,
            ctx.event_sink,
            deterministic_fallback=_DETERMINISTIC_TOOL_FALLBACK,
            timeout_seconds=get_feature_flags().phase29b_narrative_timeout_seconds,
            repair_attempts=get_feature_flags().phase29b_narrative_repair_attempts,
        )
        # 这里 await,barrier 节点会"同步"等待叙事完成才能写 next_node。
        result = await composer.compose_tool_narrative(
            task_internal_id=ctx.task_internal_id,
            graph_run_id=graph_run_id,
            context=context,
            narrative_id=narrative_id,
            generation_id=generation_id,
            generation_no=1,
        )
    except Exception as exc:  # noqa: BLE001
        # 叙事失败绝不能使主任务失败 ——
        # 这是本屏障的"硬不变量":主任务推进与 narrative 是解耦的。
        # 任何 composer 抛的异常,在本节点捕获后构造一个 fallback result,
        # 然后照常推进。
        import logging
        logging.getLogger(__name__).warning(
            "tool_narrative barrier error, falling back | task=%s | err=%s",
            ctx.task_internal_id, exc,
        )
        from app.agent_runtime.narrative_composer.schemas import NarrativeGenerationResult
        result = NarrativeGenerationResult(
            narrative_id=narrative_id,
            generation_id=generation_id,
            generation_no=1,
            success=False,
            source="deterministic",
            status="fallback",
            public_update=_DETERMINISTIC_TOOL_FALLBACK,
            failure_category="node_error",
            fallback_used=True,
        )

    # 幂等键已记录 ——
    # 写 state.pending_narrative=None,然后 next_node 指回原 Tool 想去的下一节点。
    done_keys = list(state.get("narrative_completed_logical_keys") or [])
    if logical_key not in done_keys:
        done_keys.append(logical_key)

    return {
        "pending_narrative": None,                    # 清空,下一节点不再重读
        "narrative_tool_call_id": pending.source_tool_call_id,  # 记录最近一次叙事锚点
        "narrative_completed_logical_keys": done_keys,           # 写入幂等键
        "next_node": pending.continuation_route,                 # barrier_path_map 据此选下一节点
        "narrative_result_summary": {                            # 审计用:本次结果是否成功
            "success": result.success,
            "source": result.source,
            "status": result.status,
            "narrative_id": narrative_id,
            "fallback_used": result.fallback_used,
        },
    }


def _build_task_summary_context(state: Dict[str, Any]) -> TaskSummaryNarrativeContext:
    """从 Graph State 构造压缩的 TaskSummaryContext(Phase 2.9B.6)。

    Phase 2.9B.6 修复第一个错误断点:原先直接读 ``review_result.block_count``
    等**不存在的键**(真实 review_result 结构是 ``block_issues`` / ``review_issues``
    / ``suggestions`` 列表),导致真实 blocking_issues=3 / warnings=0 /
    suggestions=1 在进入 LLM/Validator 前全部变成 0。这里统一委派
    ``_shared.summary_facts.build_summary_facts``(唯一摘要事实源,与
    task_completed.summary_facts 完全一致),再 reshape 成 Narrative 合同。
    """
    from app.agent_runtime._shared.summary_facts import build_summary_facts

    facts = build_summary_facts(dict(state))
    review = facts.get("review") or {}
    artifact = state.get("artifact") or {}

    artifact_name = _str(
        artifact.get("file_name") or artifact.get("name") or artifact.get("original_name")
    )
    artifact_size = _num(artifact.get("file_size") or artifact.get("size_bytes"))
    artifact_public_id = str(artifact.get("public_id") or artifact.get("artifact_id") or "")

    constraints = NarrativeFactConstraints()
    # 数字事实白名单:章节 / 保留章节 / 业务模块 / 审查三档 / artifact 大小。
    _add_numbers(
        constraints,
        [
            facts.get("generated_sections"),
            facts.get("kept_sections"),
            facts.get("business_modules"),
            review.get("block_count"),
            review.get("warning_count"),
            review.get("suggestion_count"),
            artifact_size,
        ],
    )
    # 产物名字面量:登记为 allowed_literal_facts(用户可见) + 豁免字面量;
    # 内部 public_id 只豁免数字,不作为展示名。
    if artifact_name:
        if artifact_name not in constraints.allowed_literal_facts:
            constraints.allowed_literal_facts.append(artifact_name)
        if artifact_name not in constraints.allowed_file_names:
            constraints.allowed_file_names.append(artifact_name)
        if artifact_name not in constraints.numeric_exempt_literals:
            constraints.numeric_exempt_literals.append(artifact_name)
    if artifact_public_id and artifact_public_id not in constraints.numeric_exempt_literals:
        constraints.numeric_exempt_literals.append(artifact_public_id)
    review_detail_literals = [
        *(review.get("block_details") or []),
        *(review.get("warning_details") or []),
        *(review.get("suggestion_details") or []),
    ]
    for literal in review_detail_literals:
        if literal and literal not in constraints.allowed_literal_facts:
            constraints.allowed_literal_facts.append(literal)

    completed_tools = []
    for name in (state.get("completed_nodes") or [])[:8]:
        completed_tools.append({"tool_name": name, "status": "success"})
    # Phase 2.9B.6: 汇总工具失败/跳过事实,供 Fact Validator 拒绝
    # 「所有工具均执行成功」类描述。从真实 Graph State 信号派生:
    #   - KnowledgeSearchTool 被跳过(kb_skip_reason)
    #   - DocxFormatCheckTool 未通过(format_check_result.status in blocked/failed)
    #   - repair/preparation fallback 存在(repair_fallback_reason /
    #     preparation_fallback_reason)
    if state.get("kb_skip_reason"):
        completed_tools.append({
            "tool_name": "KnowledgeSearchTool", "status": "skipped",
            "reason": str(state.get("kb_skip_reason")),
        })
    fmt_status = str(
        (state.get("format_check_result") or {}).get("status")
        or (state.get("format_check_result") or {}).get("level")
        or ""
    ).lower()
    if fmt_status in ("blocked", "failed"):
        completed_tools.append({
            "tool_name": "DocxFormatCheckTool", "status": "failed",
            "reason": fmt_status,
        })
    if state.get("repair_fallback_reason"):
        completed_tools.append({
            "tool_name": "RepairAgent", "status": "skipped",
            "reason": str(state.get("repair_fallback_reason")),
        })
    if state.get("preparation_fallback_reason"):
        completed_tools.append({
            "tool_name": "PreparationAgent", "status": "skipped",
            "reason": str(state.get("preparation_fallback_reason")),
        })

    # BUG FIX 2026-08-18 (B2): 提取需求文档正文摘要 + 关键章节内容摘要,
    # 让 task_summary LLM 能结合真实内容展开叙述,而非仅基于数字/文件名
    # 生成格式化模板(token 经济:需求 600 字 + 前 5 章节 × 200 字)。
    requirement_analysis = state.get("requirement_analysis") or {}
    requirement_text_excerpt: Optional[str] = None
    if isinstance(requirement_analysis, dict):
        req_text = requirement_analysis.get("text_content") or ""
        if isinstance(req_text, str) and req_text.strip():
            requirement_text_excerpt = req_text.strip()[:600]

    test_plan_content = state.get("test_plan_content") or {}
    generated_section_content_excerpts: Optional[List[Dict[str, Any]]] = None
    if isinstance(test_plan_content, dict):
        sections = test_plan_content.get("generated_sections") or []
        if isinstance(sections, list) and sections:
            excerpts: List[Dict[str, Any]] = []
            for sec in sections[:5]:  # 前 5 个章节
                if not isinstance(sec, dict):
                    continue
                content = sec.get("content") or ""
                if not isinstance(content, str) or not content.strip():
                    continue
                excerpts.append({
                    "section_id": _str(sec.get("section_id") or sec.get("id"), 64),
                    "title": _str(sec.get("title"), 80),
                    "excerpt": content.strip()[:200],
                })
            if excerpts:
                generated_section_content_excerpts = excerpts

    return TaskSummaryNarrativeContext(
        task_id=_str(state.get("task_id"), 64),
        graph_run_id=_str(state.get("graph_run_id"), 64),
        task_goal="根据需求文档和模板生成测试方案",
        # Phase 2.9B.6: task_summary 节点只在通向 finalize(completed)的路径上
        # 执行,此时 state.task_status 仍可能是 exporting/format_loss_review;
        # 最终总结必须以 completed 作为状态事实,否则 Fact Validator 会因
        # 「exporting 是过程状态」而拒绝 LLM 把任务描述成已完成。
        task_status="completed",
        completed_tools=completed_tools,
        generated_sections=facts.get("generated_sections"),
        preserved_template_sections=facts.get("kept_sections"),
        business_modules=facts.get("business_modules"),
        review={
            "blocking_issues": review.get("block_count") or 0,
            "warnings": review.get("warning_count") or 0,
            "suggestions": review.get("suggestion_count") or 0,
            "blocking_issue_details": review.get("block_details") or [],
            "warning_details": review.get("warning_details") or [],
            "suggestion_details": review.get("suggestion_details") or [],
        },
        artifact={
            "name": artifact_name,
            "format": "docx",
            "available": bool(artifact_public_id),
            "size_bytes": artifact_size,
        },
        important_decisions=[],
        repairs_performed=[],
        remaining_risks=list(review.get("block_details") or []),
        requirement_text_excerpt=requirement_text_excerpt,
        generated_section_content_excerpts=generated_section_content_excerpts,
        fact_constraints=constraints,
    )


def _num(value: Any) -> Optional[int]:
    # bool 在 Python 里也是 int 子类,这里不能让它混进数字白名单
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _str(value: Any, limit: int = 120) -> str:
    # 把任意值安全转为限长字符串(防止 prompt 撑爆 token)。
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _add_numbers(constraints: NarrativeFactConstraints, values: list[Any]) -> None:
    # 把若干数值候选添加入数字白名单,供 NarrativeValidator 强制执行。
    for v in values:
        n = _num(v)
        if n is not None and n not in constraints.allowed_numeric_facts:
            constraints.allowed_numeric_facts.append(n)


async def task_summary_narrative_node(
    state: Dict[str, Any], *, ctx: RuntimeContext
) -> Dict[str, Any]:
    """最终任务总结节点 — 在 task_completed 之前同步生成模型总结。

    三大前提退出条件:
      1) phase29b_task_summary_narrative_enabled = False → 关闭功能,直接置 done;
      2) state.task_summary_narrative_done 已经为 True → 幂等跳过(防重放);
      3) llm_client 解析为 None → MIG_NARRATIVE=true 但 bridge 不可用,跳过总结。

    生成路径:
      - _build_task_summary_context 把 Graph State 压缩成 LLM 合同;
      - NarrativeComposer + task-summary 专属 LLM Profile；
      - 仅在 LLM 输出通过叙事/事实校验后，将其完整正文写入 state.summary；
      - 失败也置 done，由下游 finalize_task 使用确定性摘要兜底。
    """
    flags = get_feature_flags()
    if not flags.phase29b_task_summary_narrative_enabled:
        # 功能关闭: 把 done 直接置 True,下游 finalize_task 正常推进。
        return {"task_summary_narrative_done": True}
    if state.get("task_summary_narrative_done"):
        # 幂等:重入 / checkpoint 重放场景。
        return {"task_summary_narrative_done": True}

    # CE-04:MIG_NARRATIVE flag 路由——flag=true 只走 ContextInvokerBridge，
    # 不可用时返回 None → 直接跳过总结（不静默回退 legacy LLMClient）。
    llm_client = _resolve_narrative_llm(
        ctx,
        call_site="test_plan.task_summary_narrative",  # 不同 call_site 可走不同 profile
    )
    if llm_client is None:
        # bridge 不可用 → 跳过总结,任务仍可以 completed,只是 narrative 是固定文案。
        return {"task_summary_narrative_done": True}

    try:
        summary_context = _build_task_summary_context(dict(state))
        composer = NarrativeComposer(
            llm_client,
            ctx.event_sink,
            deterministic_fallback=_DETERMINISTIC_TASK_SUMMARY_FALLBACK,
            timeout_seconds=flags.phase29b_narrative_timeout_seconds,
            repair_attempts=flags.phase29b_narrative_repair_attempts,
        )
        import uuid as _uuid

        narrative_id = f"tsum_{_uuid.uuid4().hex[:12]}"
        generation_id = f"tsumgen_{_uuid.uuid4().hex[:12]}"
        # prompt 构造(锁定为 prompt 契约文件,本节点只调用)
        system_prompt, user_content = build_task_summary_prompt(summary_context)

        from app.agent_runtime.narrative_composer.schemas import NarrativeGenerationRequest

        request = NarrativeGenerationRequest(
            kind="task_summary",
            narrative_id=narrative_id,
            generation_id=generation_id,
            generation_no=1,
            task_summary_context=summary_context,
            system_prompt=system_prompt,
            user_content=user_content,
        )
        # 与 barrier 不同: 这里直接调用 composer 的私有方法,因为 task_summary
        # 不需要 barrier_path_map / continuation_route,只发一个最终叙事就够了。
        result = await composer._run_generation(
            task_internal_id=ctx.task_internal_id,
            graph_run_id=str(state.get("graph_run_id") or f"run-{ctx.task_internal_id}"),
            request=request,
            context_for_fallback=summary_context,
            event_prefix="task_summary",  # 让事件类型走 TASK_SUMMARY_NARRATIVE_*
            profile=TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE,
        )
    except Exception as exc:  # noqa: BLE001
        # 叙事失败绝不能使主任务失败 ——
        # 与 barrier 一样的硬不变量:task_summary 出错,任务也要能 completed。
        import logging
        logging.getLogger(__name__).warning(
            "task_summary_narrative node error, falling back | task=%s | err=%s",
            ctx.task_internal_id, exc,
        )
        result = None
    validated_summary = ""
    if result and result.success and result.source == "llm" and result.public_update:
        # Task-summary 使用 <NARRATIVE> 合同。narrative_text 是经 decoder、schema 和
        # fact validator 共同确认后的完整三段式正文；不要降级成 200 字的 summary 摘要。
        validated_summary = str(result.public_update.narrative_text or "").strip()

    response = {
        "task_summary_narrative_done": True,  # 全部路径都置 done,让 finalize_task 推进
        "task_summary_narrative_result": {   # 审计用:本次总结的最终来源
            "success": bool(result and result.success),
            "source": (result.source if result else "deterministic"),
            "status": (result.status if result else "fallback"),
        },
    }
    if validated_summary:
        response["summary"] = validated_summary
        response["task_summary_narrative_result"]["summary_source"] = "llm"
    else:
        response["task_summary_narrative_result"]["summary_source"] = "deterministic"
    return response


__all__ = [
    "NODE_TASK_SUMMARY_NARRATIVE",
    "NODE_TOOL_NARRATIVE_BARRIER",
    "narrative_logical_key",
    "task_summary_narrative_node",
    "tool_narrative_barrier_node",
]
