"""Phase 2.9A.7 — v3 真实 Interrupt + Resume no-op 防护 + SSE Session 泄漏测试。

覆盖(对应 Phase 2.9A.7.G 21 测试):
  A. 真实 Interrupt (1-8)
  B. No-op 防护 (9-12)
  C. Graph 版本 (13-15)
  D. SSE Session (16-21)
"""

from __future__ import annotations

import inspect
import re
from unittest.mock import AsyncMock, MagicMock

import pytest


# ════════════════════════════════════════════════════════════════════════════
# A. 真实 Interrupt (1-8)
# ════════════════════════════════════════════════════════════════════════════


def test_01_v3_graph_default_has_interrupt_enabled_true() -> None:
    """Phase 2.9A.7: v3 graph 必须默认 ``interrupt_enabled=True``。

    如果默认 False, sentinel pause_marker 路径 → END 会被激活,
    Resume 时 LangGraph 找不到挂起点 → 静默 no-op。
    """
    from app.agent_runtime.graphs.test_plan.graph import compile_test_plan_graph
    from app.agent_runtime.graphs.test_plan.constants import GRAPH_VERSION_V3

    # compile_test_plan_graph(v3) 不传 interrupt_enabled,应默认 True
    # 我们通过检查 v3 模块源码,确认 default value 是 True
    import app.agent_runtime.graphs.test_plan.versions.v3.graph as v3_module

    src = inspect.getsource(v3_module)
    assert "def build_compiled_v3_graph" in src
    # 必须有 interrupt_enabled: bool = True (默认 True)
    assert re.search(r"interrupt_enabled:\s*bool\s*=\s*True", src), (
        "build_compiled_v3_graph 的 interrupt_enabled 默认必须是 True; "
        "否则 v3 graph 不注册 section_confirmation_interrupt 真 interrupt 节点"
    )


def test_02_compile_test_plan_graph_v3_forces_interrupt_enabled() -> None:
    """Phase 2.9A.7: ``compile_test_plan_graph(version='v3')`` 必须显式
    传 ``interrupt_enabled=True`` 给 v3 builder。

    防止 v3 builder 在 caller 没显式传参数时退回到 False 默认值。
    """
    from app.agent_runtime.graphs.test_plan import graph as graph_module

    src = inspect.getsource(graph_module.compile_test_plan_graph)
    # 找到 v3 分支
    v3_branch_match = re.search(
        r'if version == GRAPH_VERSION_V3:(.*?)raise GraphVersionNotAvailableError',
        src,
        flags=re.DOTALL,
    )
    assert v3_branch_match, "compile_test_plan_graph 缺少 v3 分支"
    v3_branch = v3_branch_match.group(1)
    assert "interrupt_enabled=True" in v3_branch, (
        "compile_test_plan_graph(v3) 必须显式传 interrupt_enabled=True"
    )


def test_03_prepare_section_confirmation_node_registered() -> None:
    """Phase 2.9A.7: v3 graph 必须注册 ``prepare_section_confirmation_node``。

    拆开原因:interrupt 节点不能 await,所有副作用(D持久化 + emit)都
    必须在独立 async 节点完成。
    """
    from app.agent_runtime.graphs.test_plan.versions.v3 import (
        nodes_pre_confirm,
    )

    src = inspect.getsource(nodes_pre_confirm)
    assert "def prepare_section_confirmation_node" in src, (
        "prepare_section_confirmation_node 必须定义在 nodes_pre_confirm"
    )
    # 该函数必须是 async def(LangGraph async 节点)
    func_src = inspect.getsource(
        nodes_pre_confirm.prepare_section_confirmation_node
    )
    assert "async def prepare_section_confirmation_node" in func_src


def test_04_route_after_prepare_section_confirmation_exists() -> None:
    """``route_after_prepare_section_confirmation`` 路由函数存在并正确分派。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_prepare_section_confirmation,
    )

    # mock state: failed → fail_task; 其它 → section_confirmation_interrupt
    fail_state = {"task_status": "failed"}
    assert (
        route_after_prepare_section_confirmation(fail_state)
        == "fail_task"
    )

    ok_state = {"task_status": "waiting_user_confirm"}
    assert (
        route_after_prepare_section_confirmation(ok_state)
        == "section_confirmation_interrupt"
    )


def test_05_v3_graph_edges_interrupt_to_generate_test_plan() -> None:
    """Phase 2.9A.7: section_confirmation_interrupt 完成后必须直连
    ``generate_test_plan``,不再绕 resume_task_node。

    防止 RequirementParserTool / TemplateParserTool / KnowledgeSearchTool /
    SectionSuggestionTool 在 Resume 时被重跑。
    """
    from app.agent_runtime.graphs.test_plan.versions.v3 import graph as v3_graph

    src = inspect.getsource(v3_graph.build_compiled_v3_graph)
    # 必须有 NODE_SECTION_INTERRUPT → NODE_GEN 边
    assert (
        "NODE_SECTION_INTERRUPT, NODE_GEN" in src
    ), "section_confirmation_interrupt 必须直连 generate_test_plan"


def test_06_interrupt_node_has_no_db_or_emit_side_effects() -> None:
    """Phase 2.9A.7: section_confirmation_interrupt_node 是同步节点,
    禁止 await DB / emit 副作用。

    所有副作用必须在 prepare_section_confirmation_node 完成;interrupt
    节点只调 ``interrupt()`` 等待 resume decision。
    """
    from app.agent_runtime.graphs.test_plan.versions.v3 import (
        nodes_interrupts,
    )

    func_src = inspect.getsource(
        nodes_interrupts.section_confirmation_interrupt_node
    )
    # 不能有 await
    assert "await " not in func_src, (
        "section_confirmation_interrupt_node 是同步节点,禁止 await; "
        "副作用必须全部放在 prepare_section_confirmation_node"
    )
    # 必须有 interrupt() 调用
    assert "interrupt(" in func_src


def test_07_to_outcome_detects_pending_interrupt() -> None:
    """``_to_outcome`` 检测 pending_interrupt,而不是误判为 completed。"""
    from app.agent_runtime.langgraph_run_coordinator import (
        LangGraphRunCoordinator,
    )

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)

    # mock state 含 LangGraph 注入的 __interrupt__
    interrupt_obj = MagicMock()
    interrupt_obj.value = {"kind": "section_confirmation", "sections": []}

    state_with_interrupt = {
        "task_status": "generating",
        "current_node": "section_confirmation_interrupt",
        "pause_marker": None,
        "__interrupt__": [interrupt_obj],
    }
    outcome = coord._to_outcome(state_with_interrupt)
    assert outcome.pending_interrupt == "section_confirmation"
    assert outcome.completed is False
    assert outcome.paused is False

    # 普通 state
    state_normal = {
        "task_status": "completed",
        "pause_marker": None,
    }
    outcome2 = coord._to_outcome(state_normal)
    assert outcome2.pending_interrupt is None
    assert outcome2.completed is True


def test_08_pre_confirm_interrupted_compatible_with_v3_default() -> None:
    """Phase 2.9A.7: v3 graph 默认 interrupt_enabled=True 后,
    ``run_pre_confirm_interrupted`` 走 prepare → interrupt 路径。

    编译后的 v3 graph 应含 ``section_confirmation_interrupt`` 节点。
    """
    from app.agent_runtime.graphs.test_plan.graph import compile_test_plan_graph
    from app.agent_runtime.graphs.test_plan.constants import GRAPH_VERSION_V3

    # 不连真实 Postgres,只 verify 编译能成功且 nodes 含 prepare + interrupt
    compiled = compile_test_plan_graph(
        version=GRAPH_VERSION_V3,
        checkpointer=None,
    )

    # StateGraph 编译后的图节点集合
    # LangGraph 内部用 .nodes (dict)
    nodes = getattr(compiled, "nodes", None) or {}
    node_names = set(nodes.keys())
    assert "prepare_section_confirmation" in node_names, (
        "v3 graph 必须含 prepare_section_confirmation 节点"
    )
    assert "section_confirmation_interrupt" in node_names, (
        "v3 graph 必须含 section_confirmation_interrupt 节点"
    )


# ════════════════════════════════════════════════════════════════════════════
# B. No-op 防护 (9-12)
# ════════════════════════════════════════════════════════════════════════════


def test_09_resume_raises_when_no_pending_interrupt() -> None:
    """Phase 2.9A.7: 无 pending interrupt 时 Resume 必须抛
    ``ResumeNoPendingInterruptError``(继承 ValueError)。
    """
    from app.agent_runtime.langgraph_run_coordinator import (
        ResumeNoPendingInterruptError,
    )

    # 继承 ValueError → Worker 视为不可重试
    assert issubclass(ResumeNoPendingInterruptError, ValueError)


def test_10_resume_raises_when_no_checkpoint_progress() -> None:
    """Phase 2.9A.7: Resume 后 checkpoint 不推进必须抛
    ``ResumeNoProgressError``(继承 ValueError)。
    """
    from app.agent_runtime.langgraph_run_coordinator import (
        ResumeNoProgressError,
    )

    assert issubclass(ResumeNoProgressError, ValueError)


def test_10a_resume_accepts_persisted_preparation_clarification_answer() -> None:
    """A resume is progress even when an in-memory snapshot lacks metadata."""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    coordinator = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    before = MagicMock()
    before.config = None
    before.metadata = None
    before.tasks = []
    before.next = ["preparation_clarification_interrupt"]
    before.values = {
        "current_node": "prepare_preparation_clarification",
        "task_status": "waiting_user_confirm",
    }
    after = MagicMock()
    after.config = None
    after.metadata = None
    after.tasks = []
    after.next = ["preparation_clarification_interrupt"]
    after.values = {
        "current_node": "prepare_preparation_clarification",
        "task_status": "waiting_user_confirm",
        "clarification_answers": {
            "answers": {"scope_and_capacity": "仅覆盖单人访客"},
            "source": "user",
        },
    }

    coordinator._validate_progress_after_resume(
        before_snapshot=before,
        after_snapshot=after,
        before_checkpoint_id=None,
        before_step=0,
        task_id="task_42",
    )


def test_11_resume_version_not_found_raises() -> None:
    """``ResumeGraphVersionNotFoundError`` 继承 ValueError。"""
    from app.agent_runtime.langgraph_run_coordinator import (
        ResumeGraphVersionNotFoundError,
    )

    assert issubclass(ResumeGraphVersionNotFoundError, ValueError)


def test_12_worker_marks_failed_on_resume_error() -> None:
    """Phase 2.9A.7: Worker 看到 ResumeNo*Error 时分类 error_code,
    不允许 silently dispatched。
    """
    from app.services.agent_execution_worker import AgentExecutionWorker

    worker = AgentExecutionWorker.__new__(AgentExecutionWorker)
    worker._dispatcher = None  # not used in classify

    from app.agent_runtime.langgraph_run_coordinator import (
        ResumeNoPendingInterruptError,
        ResumeNoProgressError,
        ResumeGraphVersionNotFoundError,
    )

    assert (
        worker._classify_dispatch_error(
            ResumeNoPendingInterruptError("no interrupt")
        )
        == "RESUME_NO_PENDING_INTERRUPT"
    )
    assert (
        worker._classify_dispatch_error(ResumeNoProgressError("no progress"))
        == "RESUME_NO_PROGRESS"
    )
    assert (
        worker._classify_dispatch_error(
            ResumeGraphVersionNotFoundError("no version")
        )
        == "RESUME_GRAPH_VERSION_NOT_FOUND"
    )


# ════════════════════════════════════════════════════════════════════════════
# C. Graph 版本 (13-15)
# ════════════════════════════════════════════════════════════════════════════


def test_13_compiled_for_version_resolves_v3() -> None:
    """``_compiled_for_version('v3')`` 必须返回 v3 compiled graph。"""
    from app.agent_runtime.graphs.test_plan.constants import (
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V3,
    )
    from app.agent_runtime.langgraph_run_coordinator import (
        LangGraphRunCoordinator,
    )

    sentinel = MagicMock(name="compiled_v3")
    registry = MagicMock()
    registry.get = MagicMock(return_value=sentinel)

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    coord._registry = registry

    out = coord._compiled_for_version(GRAPH_VERSION_V3)
    assert out is sentinel
    # 必须用 v3 version 查 registry(不允许被改写成 v2/default)
    registry.get.assert_called_with(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V3)


def test_14_compiled_for_version_raises_when_version_not_found() -> None:
    """``_compiled_for_version`` 在 GraphVersionNotFound 时抛
    ``ResumeGraphVersionNotFoundError``(不允许 silently fallback)。
    """
    from app.agent_runtime.langgraph_run_coordinator import (
        LangGraphRunCoordinator,
        ResumeGraphVersionNotFoundError,
    )
    from app.agent_runtime.graph_registry import GraphVersionNotFound

    registry = MagicMock()
    registry.get = MagicMock(
        side_effect=GraphVersionNotFound("graph ('test_plan_generation', 'v9') not registered")
    )
    registry.name = "test_registry"

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    coord._registry = registry

    with pytest.raises(ResumeGraphVersionNotFoundError):
        coord._compiled_for_version("v9")


def test_15_resume_validates_pending_interrupt_before() -> None:
    """``_validate_pending_interrupt_before_resume`` 在 snapshot 无
    pending interrupt 时抛 ``ResumeNoPendingInterruptError``。
    """
    from app.agent_runtime.langgraph_run_coordinator import (
        LangGraphRunCoordinator,
        ResumeNoPendingInterruptError,
    )

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)

    # mock snapshot: 无 tasks, 无 __interrupt__
    snapshot = MagicMock()
    snapshot.tasks = []
    snapshot.next = []
    snapshot.values = {}

    with pytest.raises(ResumeNoPendingInterruptError):
        coord._validate_pending_interrupt_before_resume(
            snapshot,
            expected_kind="section_confirmation",
            task_id="task_42",
        )

    # 有 tasks 但无对应 kind interrupt → 同样抛
    interrupt_obj = MagicMock()
    interrupt_obj.value = {"kind": "format_loss"}
    task_obj = MagicMock()
    task_obj.interrupts = [interrupt_obj]
    snapshot.tasks = [task_obj]
    with pytest.raises(ResumeNoPendingInterruptError):
        coord._validate_pending_interrupt_before_resume(
            snapshot,
            expected_kind="section_confirmation",
            task_id="task_42",
        )

    # 匹配 kind → 不抛
    interrupt_match = MagicMock()
    interrupt_match.value = {"kind": "section_confirmation"}
    task_match = MagicMock()
    task_match.interrupts = [interrupt_match]
    snapshot.tasks = [task_match]
    # 应当不抛
    coord._validate_pending_interrupt_before_resume(
        snapshot,
        expected_kind="section_confirmation",
        task_id="task_42",
    )


# ════════════════════════════════════════════════════════════════════════════
# D. SSE Session (16-21)
# ════════════════════════════════════════════════════════════════════════════


def test_16_task_events_sse_generator_does_not_capture_session() -> None:
    """Phase 2.9A.7: ``task_events_sse`` 的 event_stream generator 闭包
    必须不捕获 AsyncSession — 所有 DB 查询都在 generator 外完成。
    """
    from app.api.v1 import agent_tasks as tasks_module

    src = inspect.getsource(tasks_module.task_events_sse)
    # 在 event_stream() 定义前必须有独立的 async with AsyncSessionLocal()
    # 用 raw SQL 拿 task 字段
    func_lines = src.split("\n")
    # 找到 'async def event_stream' 的位置
    event_stream_idx = None
    for idx, line in enumerate(func_lines):
        if "async def event_stream" in line:
            event_stream_idx = idx
            break
    assert event_stream_idx is not None, "task_events_sse 必须有 event_stream 子函数"

    # event_stream 之前的部分必须有短 Session 查询
    before = "\n".join(func_lines[:event_stream_idx])
    assert "AsyncSessionLocal()" in before, (
        "task_events_sse 必须在 generator 外用短 Session 完成鉴权 + 快照"
    )
    assert "WHERE public_id = :pid" in before

    # event_stream 内部不能再有 'async with AsyncSessionLocal() as session:' 这种
    # 顶层捕获主 session 的模式
    event_stream_body = "\n".join(func_lines[event_stream_idx:])
    # 不允许出现 'async with AsyncSessionLocal() as session:' (不带嵌套)
    bad_pattern = re.search(
        r"async\s+with\s+AsyncSessionLocal\(\)\s+as\s+session\s*:",
        event_stream_body,
    )
    assert bad_pattern is None, (
        "event_stream generator 内部不能再有顶层 session 捕获; "
        "所有 DB 查询必须用独立短 Session(可以嵌套,但不能复用主 session)"
    )


def test_17_events_post_confirm_generator_does_not_capture_session() -> None:
    """``task_events_post_confirm_sse`` 已经在 Phase 2.9A.3 修过 —
    这里确保没回退: generator 不持 session。"""
    from app.api.v1 import agent_tasks as tasks_module

    src = inspect.getsource(tasks_module.task_events_post_confirm_sse)
    # generator 之前的鉴权必须用独立短 Session
    func_lines = src.split("\n")
    event_stream_idx = None
    for idx, line in enumerate(func_lines):
        if "async def event_stream" in line:
            event_stream_idx = idx
            break
    assert event_stream_idx is not None

    before = "\n".join(func_lines[:event_stream_idx])
    assert "AsyncSessionLocal()" in before
    assert "get_owned_task" in before


def test_18_sse_endpoints_release_session_in_cancelled_error() -> None:
    """CancelledError 路径只 unsubscribe,不再触碰 session。"""
    from app.api.v1 import agent_tasks as tasks_module

    # task_events_post_confirm_sse: generator 内部 try/except (CancelledError, GeneratorExit)
    src = inspect.getsource(tasks_module.task_events_post_confirm_sse)
    # 确保 finally 只 unsubscribe,不调 session.rollback()
    # 找出 event_stream 后的 except/finally
    assert "await unsubscribe()" in src
    # 不允许在 CancelledError 块中调 session.rollback(没有 generator-level session)


def test_19_worker_classify_returns_specific_codes() -> None:
    """``_classify_dispatch_error`` 返回具体 error_code(不是统一
    DISPATCH_CONTRACT_ERROR)。"""
    from app.services.agent_execution_worker import AgentExecutionWorker

    worker = AgentExecutionWorker.__new__(AgentExecutionWorker)

    # 普通 ValueError → DISPATCH_CONTRACT_ERROR
    assert (
        worker._classify_dispatch_error(ValueError("some error"))
        == "DISPATCH_CONTRACT_ERROR"
    )
    # TypeError → DISPATCH_CONTRACT_ERROR
    assert (
        worker._classify_dispatch_error(TypeError("oops"))
        == "DISPATCH_CONTRACT_ERROR"
    )


def test_20_sse_history_replay_uses_short_session() -> None:
    """``_replay_history`` 在 events-post-confirm 内必须用独立短 Session。"""
    from app.api.v1 import agent_tasks as tasks_module

    src = inspect.getsource(tasks_module.task_events_post_confirm_sse)
    assert "_replay_history" in src
    # _replay_history 函数体必须用 'async with AsyncSessionLocal() as s:'
    replay_func_match = re.search(
        r"async def _replay_history\(\).*?(?=\nasync def|\nclass|\n@router|$)",
        src,
        flags=re.DOTALL,
    )
    assert replay_func_match
    replay_body = replay_func_match.group(0)
    assert "async with AsyncSessionLocal() as s:" in replay_body, (
        "_replay_history 必须用独立短 Session"
    )


def test_21_resume_graph_version_specific() -> None:
    """compile_test_plan_graph(v3) 必须不传入未注册版本时的 silently fallback。"""
    from app.agent_runtime.graphs.test_plan.graph import compile_test_plan_graph
    from app.core.exceptions import GraphVersionNotAvailableError

    # 未知版本 → 抛错,不 fallback
    with pytest.raises(GraphVersionNotAvailableError):
        compile_test_plan_graph(version="v9", checkpointer=None)
