"""v3 真实业务图装配 — Phase 2.8R-D 独立版本。

设计目标(对应 docs/35 §4 + 验收五):
  * 与 v2_frozen 完全目录独立;本目录所有 .py 是 v3 专属。
  * 复用 v2 节点的实现语义(import 自 v3 子模块,而非 v2 或 v2_frozen);
    当前 v3 = v2_frozen 镜像,后续 v3 改动限于本目录。
  * 默认 ``dispatched_state_schema_version=V7``;保留与 v2 V6 read 兼容。
  * 注册到 ``graph_registry`` 时显式声明 ``GRAPH_VERSION_V3``;
    未知 version → ``GraphVersionNotAvailableError``,不 fallback。

与 v2_frozen 关键差异(2.8R-D):
  * NAME: f"{GRAPH_NAME_TEST_PLAN}_{GRAPH_VERSION_V3}" — LangGraph 内部 name 区分
  * default state_schema_version_for: V7 (V6 + 1)
  * interrupt_enabled: 仍是可选(v3 沿用 v2 interrupt 节点)
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V3,
)
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState

# 重要:从本目录 (v3) import 子模块,绝不跨目录依赖 v2/v2_frozen
from .entry_routing import (
    NODE_FAIL_TASK,
    NODE_INITIALIZE_TASK,
    NODE_RECORD_LOSS_DECISION as ENTRY_NODE_RLD,
    NODE_RESUME_TASK,
    entry_router,
)
from .nodes_interrupts import (
    NODE_FORMAT_LOSS_INTERRUPT,
    NODE_PREPARATION_CLARIFICATION_INTERRUPT,
    NODE_SECTION_CONFIRM_INTERRUPT,
)
from .nodes_narrative import (
    NODE_TASK_SUMMARY_NARRATIVE,
    NODE_TOOL_NARRATIVE_BARRIER,
    task_summary_narrative_node,
    tool_narrative_barrier_node,
)
from .nodes_post_confirm import (
    NODE_EXPORT_WORD,
    NODE_FINALIZE_TASK,
    NODE_GENERATE_TEST_PLAN,
    NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION,
    NODE_PREPARE_EXPORT,
    NODE_RECORD_LOSS_DECISION,
    export_word_node,
    finalize_task_node,
    generate_test_plan_node,
    pause_for_legacy_format_decision_node,
    prepare_export_node,
    record_loss_decision_node,
    resume_task_node,
)
from .nodes_pre_confirm import (
    NODE_PARSE_REQUIREMENT,
    NODE_PARSE_TEMPLATE,
    NODE_PAUSE_FOR_LEGACY_CONFIRM,
    NODE_PREPARE_CLARIFICATION,
    NODE_PREPARE_SECTION_CONFIRMATION,
    NODE_PREP_LEGACY_FALLBACK,
    NODE_PREP_SUBGRAPH,
    NODE_SEARCH_KNOWLEDGE,
    NODE_SUGGEST_SECTIONS,
    NODE_VALIDATE_INPUTS,
    initialize_task_node,
    parse_requirement_node,
    parse_template_node,
    pause_for_legacy_confirm_node,
    prepare_preparation_clarification_node,
    prepare_section_confirmation_node,
    prep_legacy_fallback_node,
    prep_subgraph_node,
    search_knowledge_node,
    suggest_sections_node,
    validate_inputs_node,
)
from .nodes_review_format import (
    NODE_CHECK_FORMAT_STEP,
    NODE_REGENERATE_SECTIONS_STEP,
    NODE_REPAIR_SUBGRAPH,
    NODE_REPAIR_FALLBACK,
    NODE_REVIEW_STEP,
    check_docx_format_node,
    regenerate_sections_node,
    repair_subgraph_node,
    repair_fallback_node,
    review_result_node,
)
from .nodes_terminal import NODE_CANCEL_TASK, cancel_task_node, fail_task_node
from .nodes_util import get_ctx
from .routing import (
    NODE_FAIL_TASK as ROUTE_NODE_FAIL,
    NODE_FINALIZE_TASK as ROUTE_NODE_FIN,
    NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION as ROUTE_NODE_PAUSE_FMT,
    NODE_PREPARE_EXPORT as ROUTE_NODE_PREP,
    NODE_PREPARE_CLARIFICATION as ROUTE_NODE_PREPARE_CLARIFICATION,
    NODE_PREP_LEGACY_FALLBACK as ROUTE_NODE_PREP_FALLBACK,
    NODE_PREP_SUBGRAPH as ROUTE_NODE_PREP_SUB,
    NODE_REGENERATE_SECTIONS_STEP as ROUTE_NODE_REG,
    NODE_REPAIR_SUBGRAPH as ROUTE_NODE_REPAIR,
    NODE_SEARCH_KNOWLEDGE as ROUTE_NODE_KB,
    NODE_REVIEW_STEP as ROUTE_NODE_REVIEW,
    route_after_format_check,
    route_after_parse_template,
    route_after_preparation,
    route_after_review,
    route_after_validate,
    # Phase 2.9A.9: Fail-Fast 路由
    route_after_generate_test_plan,
    route_after_export_word,
    route_after_repair,
    # Phase 2.9A.10: Review Fail-Fast
    route_after_result_review,
    # Phase 2.9B.4: tool_narrative_barrier 路由
    route_after_narrative,
)
from .routing_after_interrupt import NODE_FORMAT_LOSS_INTERRUPT as ROUTE_NODE_FORMAT_LOSS_INTERRUPT


# ── 节点包装 ───────────────────────────────────────────────────────────────


def _bind_async(node_fn):
    """把 ``async def f(state, *, ctx)`` 包成 LangGraph 节点 ``async def f(state, config)``。

    业务节点(ctx 系列)签名 ``async def f(state, *, ctx)`` 只接收显式 ctx;
    LangGraph 调度时仅传 ``(state, config)``,因此必须用 helper 把 config 解析
    为 ctx 后再透传给原函数。保留 ``__name__`` 让 LangGraph 内部异常栈可读。
    """

    async def _wrapped(state: TestPlanGraphState, config: Any) -> dict:
        # 从 LangGraph config(RunnableConfig)中取 RuntimeContext。
        ctx = get_ctx(config)
        # 直接 await 业务节点,让其返回 partial state 字典供 LangGraph 合并。
        return await node_fn(state, ctx=ctx)

    _wrapped.__name__ = node_fn.__name__
    return _wrapped


def _bind_sync(node_fn):
    """interrupt 节点必须 sync,通过此 helper 包装。

    LangGraph 的 ``interrupt()`` 调用必须发生在同步节点内,异步节点会与
    checkpointer 的状态机冲突。这里把 ``def f(state, config)`` 形状的
    interrupt 业务函数包成 LangGraph 接受的同步节点,不做额外转换。
    """

    def _wrapped(state: TestPlanGraphState, config: Any) -> dict:
        # interrupt 节点内部已自行处理 config → ctx 的解析,这里直接透传。
        return node_fn(state, config)

    _wrapped.__name__ = node_fn.__name__
    return _wrapped


# ── 名称常量 ───────────────────────────────────────────────────────────────
#
# 下面这一组别名为本图节点路由 / 边 / 消息发送处的"短名",与各 nodes_*.py 中
# 用全大写声明的节点常量同名;只是因为本文件的 routing dict 写起来很长,
# 用短别名降低行宽,保持 build_compiled_v3_graph 的可读性。
# 别名一一对应,不引入任何额外语义。


NODE_INIT = NODE_INITIALIZE_TASK                # 任务初始化节点(分配 task_id、写 started_at)
NODE_VALID = NODE_VALIDATE_INPUTS               # 输入校验(文件存在、prompt 必填等)
NODE_PARSE_REQ = NODE_PARSE_REQUIREMENT         # 解析需求文档(RequirementParserTool)
NODE_PARSE_TPL = NODE_PARSE_TEMPLATE            # 解析模板文档(TemplateParserTool)
NODE_KB = NODE_SEARCH_KNOWLEDGE                 # 公司知识库检索(KnowledgeSearchTool)
NODE_SEC = NODE_SUGGEST_SECTIONS                # 章节建议生成(SectionSuggestionTool)
NODE_PREP_SUB = NODE_PREP_SUBGRAPH              # PreparationAgent 子图入口(ask_user)
NODE_PREP_FALLBACK = NODE_PREP_LEGACY_FALLBACK  # PreparationAgent 不可用时的兜底直通
NODE_PREP_CLARIFY = NODE_PREPARE_CLARIFICATION
NODE_PREP_CONFIRM = NODE_PREPARE_SECTION_CONFIRMATION  # Phase 2.9A.7:interrupt 前置副作用节点
NODE_PAUSE_CONF = NODE_PAUSE_FOR_LEGACY_CONFIRM # sentinel pause 路径(legacy,默认不开)
NODE_SECTION_INTERRUPT = NODE_SECTION_CONFIRM_INTERRUPT  # 真 interrupt() 等待用户确认章节策略
NODE_RESUME = NODE_RESUME_TASK                  # resume 后从暂停点继续推进的入口
NODE_GEN = NODE_GENERATE_TEST_PLAN              # 调用 TestPlanGeneratorTool 生成章节内容
NODE_REVIEW = NODE_REVIEW_STEP                  # 调用 ResultReviewTool 做内容审查
NODE_REG = NODE_REGENERATE_SECTIONS_STEP        # 审查失败后局部重写章节内容
NODE_REPAIR = NODE_REPAIR_SUBGRAPH              # RepairAgent 子图(retry 失败 → 智能修复)
NODE_REPAIR_FB = NODE_REPAIR_FALLBACK           # RepairAgent 不可用时的兜底
NODE_PREP = NODE_PREPARE_EXPORT                 # 准备产物导出元数据(artifact 公共 ID 等)
NODE_WORD = NODE_EXPORT_WORD                    # 调用 WordExportTool 模板回填导出 .docx
NODE_FMT = NODE_CHECK_FORMAT_STEP               # DocxFormatCheckTool 检查 docx 完整性
NODE_PAUSE_FMT_DEC = NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION  # legacy format_loss pause
NODE_FORMAT_INTERRUPT = NODE_FORMAT_LOSS_INTERRUPT           # 真 interrupt() 等待用户确认格式丢失
NODE_RLD = NODE_RECORD_LOSS_DECISION            # 落库 format_loss 用户决策(accept/retry/reject)
NODE_FIN = NODE_FINALIZE_TASK                   # 收尾(写 task_completed、产物 public_id 等)
NODE_FAIL = NODE_FAIL_TASK                      # 失败终态(STAGE_FAILED、释放资源)
NODE_CANCEL = NODE_CANCEL_TASK                  # 取消终态(用户主动 cancel)


def build_compiled_v3_graph(
    checkpointer: Optional[Any] = None,
    *,
    interrupt_enabled: bool = True,
):
    """组装 v3 业务图并编译。

    Phase 2.8R-D 关键差异(vs v2_frozen):
      * graph name = f"{GRAPH_NAME_TEST_PLAN}_{GRAPH_VERSION_V3}"
      * 默认 state_schema_version = V7
      * 与 v2_frozen 完全目录独立

    Phase 2.9A.7: ``interrupt_enabled`` 默认 ``True``。配合
    ``graph.py:compile_test_plan_graph`` 把 v3 注册路径都开了。
    """
    g = StateGraph(TestPlanGraphState)

    # ── 节点 ───────────────────────────────────────────────────────────
    # 全部节点都用 helper 包一层:_bind_async 把 (state, ctx) 业务签名转成
    # LangGraph 接受的 (state, config);_bind_sync 用于 interrupt() 必须 sync 的节点。
    # 下文节点顺序大致按执行链路排列,便于顺着读。

    g.add_node(NODE_INIT, _bind_async(initialize_task_node))         # 任务入口:落库 AgentTask / AgentRun 行
    g.add_node(NODE_VALID, _bind_async(validate_inputs_node))         # 校验文件存在、prompt 非空
    g.add_node(NODE_PARSE_REQ, _bind_async(parse_requirement_node))   # 调 RequirementParserTool(异步 IO)
    g.add_node(NODE_PARSE_TPL, _bind_async(parse_template_node))      # 调 TemplateParserTool

    # Phase 2.9B.4: 通用 Tool 叙事同步屏障。
    # 每个 Tool 节点完成时往 state.pending_narrative 写一个待叙事片段,
    # barrier 节点 await NarrativeComposer 完成后才推进下一节点,
    # 保证前端能在 SSE 流上看到每条工具的"人在写"叙事,不会与下个 Tool 错位。
    g.add_node(NODE_TOOL_NARRATIVE_BARRIER, _bind_async(tool_narrative_barrier_node))

    g.add_node(NODE_KB, _bind_async(search_knowledge_node))           # 检索公司知识库
    g.add_node(NODE_PREP_SUB, _bind_async(prep_subgraph_node))        # PreparationAgent 子图(可选)
    g.add_node(NODE_PREP_FALLBACK, _bind_async(prep_legacy_fallback_node))  # 子图失败 / 关闭时的兜底直通
    g.add_node(NODE_PREP_CLARIFY, _bind_async(prepare_preparation_clarification_node))
    g.add_node(NODE_SEC, _bind_async(suggest_sections_node))          # 由模板 + 需求抽取生成章节建议清单

    if interrupt_enabled:
        # Phase 2.9A.7: 真 interrupt 路径需要两个节点。
        # prepare_section_confirmation 做副作用(持久化 HumanConfirmation 行 + emit 章节确认事件),
        # section_confirmation_interrupt 同步调 interrupt() 等待用户在前端选章节策略。
        # 拆开原因:interrupt 节点不能 await,所有 IO 必须在独立 async 节点完成。
        g.add_node(NODE_PREP_CONFIRM, _bind_async(prepare_section_confirmation_node))
        from .nodes_interrupts import section_confirmation_interrupt_node
        g.add_node(NODE_SECTION_INTERRUPT, _bind_sync(section_confirmation_interrupt_node))
        from .nodes_interrupts import preparation_clarification_interrupt_node
        g.add_node(
            NODE_PREPARATION_CLARIFICATION_INTERRUPT,
            _bind_sync(preparation_clarification_interrupt_node),
        )
    # legacy: 仅在 interrupt_enabled=False 时使用 sentinel pause_marker 路径,
    # resume 时由 LangGraph 的 before/after callback 重建 RUNNING 状态。
    g.add_node(NODE_PAUSE_CONF, _bind_async(pause_for_legacy_confirm_node))

    g.add_node(NODE_RESUME, _bind_async(resume_task_node))            # 真 interrupt resume 后的第一个节点
    g.add_node(NODE_GEN, _bind_async(generate_test_plan_node))        # 调 TestPlanGeneratorTool 出 section_package
    g.add_node(NODE_REVIEW, _bind_async(review_result_node))          # 调 ResultReviewTool 做内容/Schema 审查
    g.add_node(NODE_REG, _bind_async(regenerate_sections_node))       # 局部章节重写(审查没过时)
    g.add_node(NODE_REPAIR, _bind_async(repair_subgraph_node))        # RepairAgent 子图(review 通不过 → 智能修复)
    g.add_node(NODE_REPAIR_FB, _bind_async(repair_fallback_node))     # RepairAgent 失败兜底直接 PREP
    g.add_node(NODE_PREP, _bind_async(prepare_export_node))           # 准备 artifact 元数据(public_id 等)
    g.add_node(NODE_WORD, _bind_async(export_word_node))              # 调 WordExportTool 模板回填导出 .docx
    g.add_node(NODE_FMT, _bind_async(check_docx_format_node))         # DocxFormatCheckTool 完整性 + 格式损失检测

    # Phase 2.9B.4: 最终任务总结节点(task_completed 之前同步执行)。
    # 生成"任务已完成 + 关键产物 + 决策"的最终叙事;失败也走这里(走 deterministic fallback)。
    g.add_node(NODE_TASK_SUMMARY_NARRATIVE, _bind_async(task_summary_narrative_node))

    if interrupt_enabled:
        from .nodes_interrupts import format_loss_interrupt_node
        g.add_node(NODE_FORMAT_INTERRUPT, _bind_sync(format_loss_interrupt_node))  # 二次 interrupt(用户确认格式丢失)
    g.add_node(NODE_PAUSE_FMT_DEC, _bind_async(pause_for_legacy_format_decision_node))

    g.add_node(NODE_RLD, _bind_async(record_loss_decision_node))      # 落库 format_loss 用户决策结果
    g.add_node(NODE_FIN, _bind_async(finalize_task_node))             # 终态:task_completed + 产物 public_id 入库

    g.add_node(NODE_FAIL, _bind_async(fail_task_node))                # 失败终态(STAGE_FAILED)
    g.add_node(NODE_CANCEL, _bind_async(cancel_task_node))            # 取消终态

    # ── 边 ──────────────────────────────────────────────────────────────
    # LangGraph 用"边 + 条件边"描述节点之间的可达性。
    # add_edge(a, b) 是无条件 a → b;add_conditional_edges 是根据路由函数返回值选下一节点。
    # path_map 是静态约束:LangGraph 会校验路由函数返回值必须落在 path_map 键内,
    # 任何漏声明的目标会抛 GraphRuntimeError,所以本图所有可路由目标都明示列出。

    g.add_conditional_edges(
        START,
        entry_router,
        path_map={
            # 入口路由器从 thread_id 看 checkpoint:首次 → INIT,resume(用户在 interrupt)→ RESUME,
            # format_loss resume → RLD,已失败任务(>= max_attempts)直接 FAIL。
            NODE_INIT: NODE_INIT,
            NODE_RESUME: NODE_RESUME,
            NODE_RLD: NODE_RLD,
            NODE_FAIL: NODE_FAIL,
        },
    )

    g.add_edge(NODE_INIT, NODE_VALID)  # 初始化节点完成后直接进校验
    g.add_conditional_edges(
        NODE_VALID,
        route_after_validate,
        path_map={
            NODE_PARSE_REQ: NODE_PARSE_REQ,  # 校验通过 → 解析需求文档
            NODE_FAIL: NODE_FAIL,            # 校验失败(文件缺失等)→ 直接终态失败
        },
    )
    # Phase 2.9B.4: parse_requirement → tool_narrative_barrier → parse_template。
    # barrier 是同步叙事屏障:RequirementParser 叙事完成后才进入 TemplateParser。
    # 下面 barrier_path_map 是 barrier 节点 conditional_edges 的允许目标,
    # 集中管理,避免每个 tool 调用点都重复声明一遍。
    barrier_path_map = {
        NODE_PARSE_TPL: NODE_PARSE_TPL,
        ROUTE_NODE_PREP_SUB: NODE_PREP_SUB,
        ROUTE_NODE_PREPARE_CLARIFICATION: NODE_PREP_CLARIFY,
        ROUTE_NODE_KB: NODE_KB,
        NODE_SEC: NODE_SEC,
        NODE_PREPARE_SECTION_CONFIRMATION: (
            NODE_PREP_CONFIRM if interrupt_enabled else NODE_PAUSE_CONF
        ),
        ROUTE_NODE_REVIEW: NODE_REVIEW,
        ROUTE_NODE_REG: NODE_REG,
        ROUTE_NODE_REPAIR: NODE_REPAIR,
        ROUTE_NODE_PREP: NODE_PREP,
        NODE_CHECK_FORMAT_STEP: NODE_FMT,
        ROUTE_NODE_FIN: NODE_TASK_SUMMARY_NARRATIVE,
        ROUTE_NODE_PAUSE_FMT: (
            NODE_FORMAT_INTERRUPT if interrupt_enabled else NODE_PAUSE_FMT_DEC
        ),
        # Phase 2.9A.X: export_word_node 检测到 secondary 结构丢失时,
        # 走 format_loss_review 让用户决定继续 / 重试 / 放弃,而不是直接 FAILED。
        ROUTE_NODE_FORMAT_LOSS_INTERRUPT: (
            NODE_FORMAT_INTERRUPT if interrupt_enabled else NODE_PAUSE_FMT_DEC
        ),
        ROUTE_NODE_FAIL: NODE_FAIL,
    }

    g.add_edge(NODE_PARSE_REQ, NODE_TOOL_NARRATIVE_BARRIER)  # RequirementParser 完 → 进入 barrier
    g.add_conditional_edges(
        NODE_TOOL_NARRATIVE_BARRIER,
        # route_after_narrative 读 state.pending_narrative.continuation_route,
        # 决定 barrier 后去下一个 Tool/节点;支持 KB / Prep / Interrupt 等多种目标。
        route_after_narrative,
        path_map=barrier_path_map,
    )
    g.add_edge(NODE_PARSE_TPL, NODE_TOOL_NARRATIVE_BARRIER)         # TemplateParser 完 → barrier
    g.add_conditional_edges(
        NODE_PREP_SUB,
        route_after_preparation,
        path_map={
            NODE_PREP_FALLBACK: NODE_PREP_FALLBACK,
            NODE_KB: NODE_KB,
            NODE_SEC: NODE_SEC,
            NODE_PREP_CLARIFY: NODE_PREP_CLARIFY,
        },
    )
    g.add_edge(NODE_PREP_FALLBACK, NODE_SEC)                        # legacy fallback remains non-blocking
    g.add_edge(NODE_KB, NODE_TOOL_NARRATIVE_BARRIER)                # 知识库检索完 → barrier

    if interrupt_enabled:
        # Phase 2.9A.7: 真 interrupt 路径拓扑 —
        #   suggest_sections → tool_narrative_barrier → prepare_section_confirmation(副作用)
        #     → section_confirmation_interrupt(同步 wait)
        #     → generate_test_plan(直连,绕开 resume_task_node 因为
        #       LangGraph 会从 interrupt 节点自动 resume 后续节点)。
        g.add_edge(NODE_SEC, NODE_TOOL_NARRATIVE_BARRIER)
        # prepare 节点若返回 task_status=failed → 走 FAIL_TASK 路由
        # 借助 conditional edges 而非无条件 continue;
        # 否则 prepare 完成后直连 interrupt 节点。
        from .routing import route_after_prepare_section_confirmation
        g.add_conditional_edges(
            NODE_PREP_CONFIRM,
            route_after_prepare_section_confirmation,
            path_map={
                NODE_SECTION_INTERRUPT: NODE_SECTION_INTERRUPT,
                NODE_FAIL: NODE_FAIL,
            },
        )
        # 关键修复: interrupt 完成后直连 generate_test_plan,
        # 不再绕 resume_task_node(后者是为 sentinel pause 路径服务的)。
        g.add_edge(NODE_SECTION_INTERRUPT, NODE_GEN)
        g.add_edge(NODE_PREP_CLARIFY, NODE_PREPARATION_CLARIFICATION_INTERRUPT)
        g.add_edge(NODE_PREPARATION_CLARIFICATION_INTERRUPT, NODE_PREP_SUB)
    else:
        # Legacy / Phase 2.1 sentinel 路径 — pause_marker → END。
        # 保留向后兼容,生产 v3 默认走 interrupt 路径。
        g.add_edge(NODE_SEC, NODE_TOOL_NARRATIVE_BARRIER)
        g.add_edge(NODE_PAUSE_CONF, END)  # sentinel 路径直接 END,worker 后续走 before/after callback 重建 RUNNING

    g.add_edge(NODE_RESUME, NODE_GEN)                               # 真 interrupt resume 后直连 GEN
    # Phase 2.9A.9: 之前是无条件 NODE_GEN → NODE_REVIEW,失败也会级联 review。
    # 这里改成显式 add_edge 到 barrier,barrier 内 route_after_narrative 根据 task_status
    # 决定走 review / fail_task,避免 Tool 失败被级联到 review。
    g.add_edge(NODE_GEN, NODE_TOOL_NARRATIVE_BARRIER)
    g.add_edge(NODE_REVIEW, NODE_TOOL_NARRATIVE_BARRIER)            # 审查完 → 叙事屏障
    g.add_edge(NODE_REG, NODE_REVIEW)                               # 局部重写完 → 重新进 review
    g.add_conditional_edges(
        NODE_REPAIR,
        route_after_repair,
        path_map={
            ROUTE_NODE_PREP: NODE_PREP,
            NODE_REPAIR_FB: NODE_REPAIR_FB,
            NODE_FAIL: NODE_FAIL,
        },
    )
    # A deterministic fallback must be re-reviewed before export; otherwise a
    # known blocking document could bypass the ResultReviewTool gate.
    g.add_edge(NODE_REPAIR_FB, NODE_REVIEW)
    g.add_edge(NODE_PREP, NODE_WORD)                                # 准备元数据完 → 调导出工具
    # Phase 2.9A.9: 之前是无条件 NODE_WORD → NODE_FMT,WordExportTool 失败
    # 也会级联 format_check。改为 conditional:失败 → fail_task。
    g.add_edge(NODE_WORD, NODE_TOOL_NARRATIVE_BARRIER)

    if interrupt_enabled:
        # format_loss 路径同样进 barrier,barrier 路由根据 pending_format_losses 决定走
        # format_loss_interrupt / finalize / fail_task,保证 LLM narrative 与用户决策
        # 在前端展示上一致。
        g.add_edge(NODE_FMT, NODE_TOOL_NARRATIVE_BARRIER)
        from .routing_after_interrupt import route_after_format_interrupt
        g.add_conditional_edges(
            NODE_FORMAT_INTERRUPT,
            # 用户在二级 interrupt 的决策(accept/retry/reject)决定下一节点:
            # accept → 进入 task_summary 准备收尾;retry → 重新走 PREP 重出文档;
            # 不可恢复失败 → FAIL_TASK。
            route_after_format_interrupt,
            path_map={
                NODE_FIN: NODE_TASK_SUMMARY_NARRATIVE,
                NODE_PREP: NODE_PREP,
                NODE_FAIL: NODE_FAIL,
            },
        )
    else:
        # legacy sentinel: format_loss 决策 record_loss_decision 后直连 task_summary
        g.add_edge(NODE_FMT, NODE_TOOL_NARRATIVE_BARRIER)
        g.add_edge(NODE_PAUSE_FMT_DEC, END)
        g.add_edge(NODE_RLD, NODE_TASK_SUMMARY_NARRATIVE)

    # Phase 2.9B.4: 任务总结完成后才 finalize(task_completed)。
    # 顺序硬约束:NARRATIVE → FIN → END,保证前端收尾阶段能拿到 LLM / fallback 总结。
    g.add_edge(NODE_TASK_SUMMARY_NARRATIVE, NODE_FIN)
    g.add_edge(NODE_FIN, END)
    g.add_edge(NODE_FAIL, END)
    g.add_edge(NODE_CANCEL, END)

    # Phase 2.8R-D v3: recursion_limit 由调用方 LangGraphRunCoordinator 注入(避免这里硬编码)
    return g.compile(
        checkpointer=checkpointer,                                # 持久化 POSTGRES checkpointer(可选 None → in-memory)
        # graph.name 决定 LangGraph checkpoint 内 graph_id 与多版本路由;
        # name 含 V3 后缀可与 v2_frozen 在同一 store 中区分,支持版本隔离。
        name=f"{GRAPH_NAME_TEST_PLAN}_{GRAPH_VERSION_V3}",
    )


def build_test_plan_v3_graph(
    checkpointer: Optional[Any] = None,
    *,
    interrupt_enabled: bool = True,
):
    """Phase 2.8R-D v3 业务图 facade。

    用法与 ``build_test_plan_v2_graph`` 完全一致;区别在于:
      * graph name 含 v3 后缀(LangGraph 内部 name)
      * state 默认 schema_version_for("v3") == V7
      * 目录独立,后续 v3 改动限于 versions/v3/

    Phase 2.9A.7: ``interrupt_enabled`` 默认改为 ``True``,v3 编译时
    注册 ``section_confirmation_interrupt`` + ``format_loss_interrupt``
    真 interrupt 节点。fallback 路径(``pause_for_legacy_confirm`` /
    ``pause_for_legacy_format_decision``)仍保留,但默认不再走 —
    否则 Resume 时 LangGraph 找不到 pending interrupt 而重跑整个图,
    最终 task 永远停在 ``waiting_user_confirm``,Worker 记 dispatched
    但 TestPlanGeneratorTool 不开始(静默 no-op)。
    """
    # 简单 facade:目前 v3 编译逻辑没差,统一经过 build_compiled_v3_graph 方便
    # 测试 / registry 用同一个名字引用。后续若加 dispatch / 灰度版本,只需
    # 在这里分流即可,调用方不必改。
    return build_compiled_v3_graph(
        checkpointer=checkpointer,
        interrupt_enabled=interrupt_enabled,
    )


__all__ = [
    "build_compiled_v3_graph",
    "build_test_plan_v3_graph",
]
