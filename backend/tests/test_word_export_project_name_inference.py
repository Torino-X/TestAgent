"""WordExportTool._extract_project_name best-effort 兜底测试。

BUG FIX 2026-08-18 (D):_extract_project_name 必须把 LLM 推断用
asyncio.wait_for 包住(总预算 3s),失败 / 超时走模板 basename → task_id
兜底,Word 导出不被项目名阻塞。本文件覆盖:
  * 优先级 1:requirement_analysis.project_name 直接返回
  * 优先级 2:LLM 推断成功返回推断名
  * 优先级 3:LLM 失败 + template_basename → basename stem
  * 优先级 4:LLM 失败 + 无 basename → task_id
  * 优先级 5:LLM 失败 + 无 basename + 无 task_id → "未命名项目"
  * 超时:asyncio.wait_for 超时 → fallback
  * basename 无 .docx 后缀:原样返回
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.context import AgentContext
from app.tools.word_export_tool import WordExportTool


def _ctx(
    *,
    project_name: str | None = None,
    text_content: str = "",
    user_prompt: str = "",
    task_id: str = "task_default",
    document_name: str = "",
) -> AgentContext:
    """构造最小可用的 AgentContext。"""
    req: dict = {}
    if text_content:
        req["text_content"] = text_content
    if document_name:
        req["document_name"] = document_name
    if project_name is not None:
        req["project_name"] = project_name
    return AgentContext(
        task_id=task_id,
        conversation_id="conv_x",
        user_id="user_x",
        session=SimpleNamespace(),
        requirement_analysis=req or None,
        user_prompt=user_prompt,
    )


# ── 优先级 1:requirement_analysis.project_name ─────────────────────────


@pytest.mark.asyncio
async def test_priority1_requirement_analysis_project_name_returned_directly():
    """req.project_name 非空时,直接返回(不调用 LLM)。"""
    ctx = _ctx(project_name="已有项目名", text_content="很长文本" * 100)
    with patch.object(
        WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
    ) as mock_infer:
        mock_infer.return_value = "不应该调用"
        result = await WordExportTool._extract_project_name(ctx)
    assert result == "已有项目名"
    mock_infer.assert_not_called()


# ── 优先级 2:LLM 推断成功 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_priority2_llm_inferred_name_returned():
    """LLM 推断返回非空字符串时,返回推断值。"""
    ctx = _ctx(text_content="这是一个觅迅测试系统的需求文档" * 20)
    with patch.object(
        WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
    ) as mock_infer:
        mock_infer.return_value = "觅迅测试系统"
        result = await WordExportTool._extract_project_name(ctx)
    assert result == "觅迅测试系统"


# ── 优先级 3:LLM 失败 + template_basename → basename ───────────────────


@pytest.mark.asyncio
async def test_priority3_template_basename_used_when_llm_fails():
    """LLM 返回 None + template_basename 不像模板名 → 去 .docx 后缀的 stem。

    BUG FIX 2026-08-18 (C): 当 template_basename 含"测试方案"/"模板"/
    日期哈希前缀/版本号前缀等"模板命名特征"时,跳过并 fallback 到
    task_id(避免不规范模板名污染产物文件名)。
    """
    ctx = _ctx(text_content="需求" * 50, task_id="task_priority3")
    with patch.object(
        WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
    ) as mock_infer:
        mock_infer.return_value = None
        # "觅迅系统" 不像模板文件名,正常用作项目名兜底
        result = await WordExportTool._extract_project_name(
            ctx, "觅迅系统.docx"
        )
    assert result == "觅迅系统"


@pytest.mark.asyncio
async def test_template_basename_without_docx_suffix():
    """basename 没有 .docx 后缀时,原样返回(不强行补 .docx)。

    BUG FIX 2026-08-18 (C): 仅当 basename 不像模板文件名时才用作兜底。
    """
    ctx = _ctx(text_content="需求" * 50)
    with patch.object(
        WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
    ) as mock_infer:
        mock_infer.return_value = None
        result = await WordExportTool._extract_project_name(ctx, "觅迅系统")
    assert result == "觅迅系统"


# ── 优先级 4:LLM 失败 + 无 basename → task_id ──────────────────────────


@pytest.mark.asyncio
async def test_priority4_task_id_used_when_llm_fails_no_basename():
    """LLM 返回 None + 无 template_basename → task_id。"""
    ctx = _ctx(text_content="需求" * 50, task_id="task_xyz")
    with patch.object(
        WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
    ) as mock_infer:
        mock_infer.return_value = None
        result = await WordExportTool._extract_project_name(ctx)
    assert result == "xyz"


# ── 优先级 5:LLM 失败 + 无 basename + 无 task_id → "未命名项目" ─────────


@pytest.mark.asyncio
async def test_priority5_unnamed_project_when_all_missing():
    """LLM 失败 + 无 basename + 无 task_id → '未命名项目'(硬编码兜底)。"""
    ctx = _ctx(text_content="需求" * 50, task_id="")
    with patch.object(
        WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
    ) as mock_infer:
        mock_infer.return_value = None
        result = await WordExportTool._extract_project_name(ctx)
    assert result == "未命名项目"


# ── 超时控制 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_falls_back_to_basename():
    """asyncio.wait_for 超时 → 走 fallback(template_basename 不像模板时优先)。

    BUG FIX 2026-08-18 (C): 若 template_basename 像模板文件名(命中
    "测试方案"/"模板"/含日期哈希/含版本号),跳过 fallback 到 task_id。
    """
    ctx = _ctx(text_content="需求" * 50, task_id="task_timeout")

    async def _slow_infer(*args, **kwargs):
        await asyncio.sleep(10)  # 远超 wait_for=3s

    with patch.object(
        WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
    ) as mock_infer:
        mock_infer.side_effect = _slow_infer
        # "觅迅系统_v2" 不含模板关键词,且 v\d+_ 形式 → 但 v\d+ 是版本号前缀
        # 用"觅迅系统"避开所有模板特征
        result = await WordExportTool._extract_project_name(
            ctx, "觅迅系统.docx"
        )
    assert result == "觅迅系统"


@pytest.mark.asyncio
async def test_exception_in_inference_falls_back_to_task_id():
    """LLM 推断抛任意异常 → fallback(短前缀 task_safe → safe)。"""
    ctx = _ctx(text_content="需求" * 50, task_id="task_safe")

    async def _boom(*args, **kwargs):
        raise RuntimeError("bridge unavailable")

    with patch.object(
        WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
    ) as mock_infer:
        mock_infer.side_effect = _boom
        result = await WordExportTool._extract_project_name(ctx)
    assert result == "safe"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_requirement_document_name():
    """用户场景:LLM/MIG 项目名推断失败时,从需求文件名提取项目名。"""
    ctx = _ctx(
        text_content="需求" * 50,
        task_id="task_f0568c63",
        document_name="01_智慧校园预约与签到系统_需求说明书.docx",
    )

    async def _bridge_unavailable(*args, **kwargs):
        return None

    with patch.object(WordExportTool, "_llm_infer_project_name", _bridge_unavailable):
        result = await WordExportTool._extract_project_name(
            ctx, "00_PlanWise_QA_测试方案模板.docx"
        )

    assert result == "智慧校园预约与签到系统"
    assert result != "f0568c63"


# ── wait_for 真实超时边界 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_wait_for_timeout_uses_fallback():
    """端到端:不 mock _llm_infer_project_name,用真实 asyncio.wait_for,
    LLM 慢于总预算时进入 fallback 路径。

    BUG FIX 2026-08-19：总预算从 3s 改为 30s（常量）。本 case 用
    monkeypatch 把总预算临时压回 0.5s 复现旧 5s sleep 的语义，既
    触发真实 wait_for 超时分支又不拖慢测试套。
    """
    from app.tools import word_export_tool

    ctx = _ctx(text_content="需求" * 50, task_id="task_e2e")

    async def _slow_infer(_ctx, _snippet):
        await asyncio.sleep(2)  # 远超压回后的 0.5s 总预算
        return "should_not_return"

    original = word_export_tool._PROJECT_TITLE_TOTAL_BUDGET_S
    word_export_tool._PROJECT_TITLE_TOTAL_BUDGET_S = 0.5
    try:
        with patch.object(WordExportTool, "_llm_infer_project_name", _slow_infer):
            result = await WordExportTool._extract_project_name(ctx)
    finally:
        word_export_tool._PROJECT_TITLE_TOTAL_BUDGET_S = original
    assert result == "e2e"


# ── BUG FIX 2026-08-18 (C): 模板 basename 防御性过滤 ──────────────────
#
# 模板 basename 仅当不像"模板文件名"时作为项目名兜底。
# 判定标准:_looks_like_template_name() 检查关键词/日期哈希/版本号前缀。


class TestLooksLikeTemplateName:
    """模块级 helper ``_looks_like_template_name`` 单元测试。"""

    def test_keyword_测试方案(self):
        from app.tools.word_export_tool import _looks_like_template_name
        assert _looks_like_template_name("觅迅系统测试方案模板.docx") is True
        assert _looks_like_template_name("Test Plan Template.docx") is True
        assert _looks_like_template_name("PlanWise_QA_测试方案.docx") is True

    def test_keyword_模板(self):
        from app.tools.word_export_tool import _looks_like_template_name
        assert _looks_like_template_name("00_PlanWise_QA_测试方案模板.docx") is True
        assert _looks_like_template_name("我的模板.docx") is True

    def test_date_hash_prefix(self):
        """local_storage.safe_filename 风格前缀 — 8 日期 + hex 后缀 + 下划线。

        注:必须有``_``结尾(后面有内容)才判定为模板名;
        仅有``20260818_abc12345.docx``(没有下划线+内容)不是系统命名,
        可能是有意手工命名,不判定为模板。
        """
        from app.tools.word_export_tool import _looks_like_template_name
        assert _looks_like_template_name("20260818_c25a1edf_觅迅.docx") is True
        assert _looks_like_template_name("20260818_abc12345_觅迅系统.docx") is True
        # 反例:不是系统命名格式
        assert _looks_like_template_name("20260818_abc12345.docx") is False

    def test_version_prefix(self):
        from app.tools.word_export_tool import _looks_like_template_name
        assert _looks_like_template_name("v1_觅迅系统.docx") is True
        assert _looks_like_template_name("V2_plan.docx") is True

    def test_normal_project_name_not_flagged(self):
        """合法项目名(无模板特征)应返回 False。"""
        from app.tools.word_export_tool import _looks_like_template_name
        assert _looks_like_template_name("觅迅系统.docx") is False
        assert _looks_like_template_name("PlanWise.docx") is False
        assert _looks_like_template_name("觅迅测试管理平台.docx") is False

    def test_empty_or_none_conservatively_returns_true(self):
        """空值/None 默认保守(当作模板名),走 task_id 兜底。"""
        from app.tools.word_export_tool import _looks_like_template_name
        assert _looks_like_template_name("") is True
        assert _looks_like_template_name(None) is True


class TestProjectNameFallbackSkipsTemplateBasename:
    """``_extract_project_name`` 模板 basename 像模板名时跳过 → task_id 兜底。"""

    @pytest.mark.asyncio
    async def test_user_scenario_00_planwise_qa_template(self):
        """用户场景:模板命名 ``00_PlanWise_QA_测试方案模板.docx`` →
        project_name 不应被污染,而是用 task_id 兜底。
        """
        ctx = _ctx(text_content="需求" * 50, task_id="task_xyz")
        with patch.object(
            WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
        ) as mock_infer:
            mock_infer.return_value = None
            result = await WordExportTool._extract_project_name(
                ctx, "00_PlanWise_QA_测试方案模板.docx"
            )
        assert result == "xyz"
        assert "00_" not in result
        assert "PlanWise_QA" not in result
        assert "模板" not in result

    @pytest.mark.asyncio
    async def test_template_with_testplan_keyword_skipped(self):
        """basename 含"测试方案" → 跳过 → task_id。"""
        ctx = _ctx(text_content="需求" * 50, task_id="task_keyword")
        with patch.object(
            WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
        ) as mock_infer:
            mock_infer.return_value = None
            result = await WordExportTool._extract_project_name(
                ctx, "我的项目测试方案.docx"
            )
        assert result == "keyword"

    @pytest.mark.asyncio
    async def test_template_with_date_hash_prefix_skipped(self):
        """basename 以日期+哈希开头 → 跳过 → task_id(系统命名非用户输入)。"""
        ctx = _ctx(text_content="需求" * 50, task_id="task_dh")
        with patch.object(
            WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
        ) as mock_infer:
            mock_infer.return_value = None
            result = await WordExportTool._extract_project_name(
                ctx, "20260818_abc12345_觅迅.docx"
            )
        assert result == "dh"

    @pytest.mark.asyncio
    async def test_template_with_version_prefix_skipped(self):
        """basename 以 v\\d+_ 开头 → 跳过 → task_id。"""
        ctx = _ctx(text_content="需求" * 50, task_id="task_v")
        with patch.object(
            WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
        ) as mock_infer:
            mock_infer.return_value = None
            result = await WordExportTool._extract_project_name(
                ctx, "v2_觅迅系统.docx"
            )
        assert result == "v"

    @pytest.mark.asyncio
    async def test_clean_basename_used_as_project_name(self):
        """干净的 basename(无模板特征)仍可用作项目名。"""
        ctx = _ctx(text_content="需求" * 50, task_id="task_clean")
        with patch.object(
            WordExportTool, "_llm_infer_project_name", new_callable=AsyncMock
        ) as mock_infer:
            mock_infer.return_value = None
            result = await WordExportTool._extract_project_name(
                ctx, "觅迅系统.docx"
            )
        assert result == "觅迅系统"


# ── BUG FIX 2026-08-19：项目名 LLM 推断超时预算 ────────────────────────────
#
# 原值 timeout_override=2s 对 DeepSeek 过紧，导致主模型调用超时产生
# ERROR 日志（虽然走 fallback 不阻塞导出）。统一抽常量改为 20s
# （内层 timeout_override）+ 30s（外层 asyncio.wait_for），与 agent_runtime
# 其他 LLM 调用对齐。本文件回归覆盖：
#   * 常量值已正确改大
#   * 慢于 20s 但短于 30s 的 LLM 推断能成功返回（不再被错杀）
#   * 慢于 30s 的 LLM 推断仍走 fallback 链


def test_project_title_timeout_constants_are_loosened():
    from app.tools import word_export_tool

    assert word_export_tool._PROJECT_TITLE_LLM_TIMEOUT_OVERRIDE_S == 20
    assert word_export_tool._PROJECT_TITLE_TOTAL_BUDGET_S >= 20.0


def test_read_docx_page_count_uses_word_statistics(monkeypatch, tmp_path):
    from app.tools import word_export_tool
    import importlib

    path = tmp_path / "out.docx"
    path.write_bytes(b"placeholder")

    state = {"closed": False, "quit": False, "opened": ""}

    class FakeDoc:
        def ComputeStatistics(self, statistic):
            assert statistic == 2
            return 12

        def Close(self, save_changes):
            assert save_changes is False
            state["closed"] = True

    class FakeDocuments:
        def Open(self, path_arg, **kwargs):
            state["opened"] = path_arg
            assert kwargs["ReadOnly"] is True
            assert kwargs["AddToRecentFiles"] is False
            assert kwargs["Visible"] is False
            return FakeDoc()

    class FakeWord:
        Documents = FakeDocuments()
        Visible = True
        DisplayAlerts = 1

        def Quit(self):
            state["quit"] = True

    fake_client = SimpleNamespace(DispatchEx=lambda name: FakeWord())
    real_import_module = importlib.import_module

    def fake_import_module(name):
        if name == "win32com.client":
            return fake_client
        return real_import_module(name)

    monkeypatch.setattr(word_export_tool.os, "name", "nt")
    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    assert WordExportTool._read_docx_page_count(str(path)) == 12
    assert state["closed"] is True
    assert state["quit"] is True
    assert state["opened"].endswith("out.docx")


@pytest.mark.asyncio
async def test_slow_but_within_budget_inference_returns_value():
    """LLM 推断耗时 5s(超过旧 3s 总预算,但在新 30s 内)→ 不应误入 fallback。"""
    ctx = _ctx(text_content="需求" * 50, task_id="task_slow_ok")

    async def _moderate_infer(_ctx, _snippet):
        await asyncio.sleep(0.2)  # 短延迟模拟，避免拖慢测试套
        return "推断的项目名"

    with patch.object(WordExportTool, "_llm_infer_project_name", _moderate_infer):
        result = await WordExportTool._extract_project_name(ctx)
    assert result == "推断的项目名"


@pytest.mark.asyncio
async def test_exceeding_new_total_budget_falls_back():
    """LLM 耗时超过总预算 → 仍走 fallback(task_id 短前缀)。

    BUG FIX 2026-08-19：总预算抽常量后，用 monkeypatch 压回 0.5s
    复现超时分支，避免拖慢测试套。
    """
    from app.tools import word_export_tool

    ctx = _ctx(text_content="需求" * 50, task_id="task_budget_out")

    async def _too_slow(_ctx, _snippet):
        await asyncio.sleep(2)  # 远超压回后的 0.5s 总预算
        return "should_not_return"

    original = word_export_tool._PROJECT_TITLE_TOTAL_BUDGET_S
    word_export_tool._PROJECT_TITLE_TOTAL_BUDGET_S = 0.5
    try:
        with patch.object(WordExportTool, "_llm_infer_project_name", _too_slow):
            result = await WordExportTool._extract_project_name(ctx)
    finally:
        word_export_tool._PROJECT_TITLE_TOTAL_BUDGET_S = original
    assert result == "budget_o"  # task_budget_out 去掉 "task_" 取 8 字符 = "budget_o"
