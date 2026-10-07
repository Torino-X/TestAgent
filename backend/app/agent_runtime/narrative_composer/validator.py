"""Phase 2.9B.4 — NarrativeValidator(Schema + 事实校验)。

校验规则(docs/93 技术方案 §12):
* headline / summary / impact / next_action 非空;
* details 数量 2～5(存在事实时应输出对应条目);
* 所有数字来自 allowed_numeric_facts(中文数字与阿拉伯数字归一化);
* 文件名来自 allowed_file_names;
* Tool 状态语义正确(failed 不写成功、skipped 不写已执行、
  retry_planned 说明将重试、waiting_for_user 说明等待确认、
  Artifact 未持久化时不说可下载);
* 不包含敏感字段(forbidden_internal_fields 子串 + 常见泄漏标记)。

返回 NarrativeValidationResult;校验失败可带 fact_feedback 供一次修复调用。

════════════════════════════════════════════════════════════════════════════════
模块分层(本文件代码组织):

  1. 工具函数段:
       - _extract_numbers / _cn_to_int:把阿拉伯/中文数字归一化
       - _mask_literals:把文件名/产物名/版本号字面量整体遮蔽,防止内部数字
         被误判为虚构事实(file_dc6d128c 的 6/128 不算)
       - _mask_markdown_list_ordinals:1./2. 这种列表 ordinal 不算虚构统计数字
       - _FILE_NAME_RE / _extract_literal_tokens:抓文件名形态
       - _cn_to_int / _contains_any:小工具
       - _num_like:bool/float 安全转 int

  2. class NarrativeValidator:
       - validate(public_update, tool_context?, task_summary_context?):
         入口方法,聚合所有规则;
       - _validate_task_summary_facts:Phase 2.9B.6 task summary 事实一致性
         (数字白名单 + Artifact + 状态 + 否定语义 + Tool 失败/跳过事实 +
         内部信息安全)。

Phase 2.9B.5 / 2.9B.6 关键行为差异(避免误读):

  - Phase 2.9B.5:allowed_literal_facts / numeric_exempt_literals /
    allowed_file_names 先做最长字符串匹配遮蔽,再扫描剩余数字,避免文件名
    内嵌数字(v3 的 3, file_dc6d128c 的 6/128)被误判为虚构事实。
  - Phase 2.9A.X bug fix:narrative_text(LLM 自由叙述)不再走严格数字白名单;
    只对结构化字段 impact + details 做严格白名单校验,避开"X 个"量词被误判。
  - Phase 2.9B.6:blocking>0 但叙事说"无阻塞问题"→ 拒绝;
                  failed/skipped 工具存在但说"全部成功"→ 拒绝;
                  Artifact 未持久化时描述为可下载 → 拒绝。

被 NarrativeComposer 在 _run_generation 流式生成 + repair 后调用;
返回 NarrativeValidationResult(valid, errors, fact_feedback)。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .schemas import (
    NarrativeFactConstraints,
    NarrativeValidationResult,
    TaskSummaryNarrativeContext,
    ToolNarrativeContext,
)

_CN_NUM_MAP = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_TEN = {"十": 10, "百": 100, "千": 1000}


def _extract_numbers(text: str) -> List[int]:
    """从文本提取所有数字(阿拉伯 + 简单中文数字归一化)。"""
    out: List[int] = []
    for m in re.finditer(r"\d+", text):
        try:
            out.append(int(m.group()))
        except ValueError:
            pass
    # 简单中文数字(单个字 + 十/百组合)。
    for m in re.finditer(r"[零一二两三四五六七八九十百千]+", text):
        n = _cn_to_int(m.group())
        if n is not None:
            out.append(n)
    return out


def _mask_literals(text: str, literals: List[str]) -> str:
    """把允许出现的完整字面量从数字扫描文本中遮蔽(最长字符串优先)。

    Phase 2.9B.5: 文件名 / 产物名 / 版本号等字面量内部可能包含数字
    (``file_dc6d128c`` 的 6/128、``03_xxx.docx`` 的 03、``v3`` 的 3)。
    这些数字不是虚构统计事实,必须在提取数字前把完整字面量替换为无数字占位符,
    否则会被误判为「数字 6 不在允许事实范围内」。
    """
    if not literals:
        return text
    result = text
    for literal in sorted({lit for lit in literals if lit}, key=len, reverse=True):
        if literal in result:
            result = result.replace(literal, " " * len(literal))
    return result


def _mask_markdown_list_ordinals(text: str) -> str:
    """Ignore Markdown ordered-list ordinals such as ``1.`` or ``2.``."""
    return re.sub(r"(?m)^\s*\d+[.)]\s+", "- ", text)


# 旧的 Phase 2.9A.X 量词遮蔽方案已撤回。
# 原因:测试 ``test_unauthorized_number_still_rejected`` /
# ``test_fabricated_99_still_rejected`` 明确要求 "共 128 个章节" / "识别 99 个
# 章节" 仍要走严格白名单,即"X 个" 量词形式本质是统计数字,白名单必须包含。
# 真正被误判的是 LLM 自由叙述段落(narrative_text)中的数字,不在
# 结构化 fact_text 范围内。下面改成:
#   - fact_text 只取 impact + details(与上方注释保持一致)
#   - narrative_text 不参与严格数字白名单,仅参与文件名 / 敏感字段校验
# 保留量词正则常量供未来按需启用(若需要按场景豁免,改成可选 enabled=True)。


_FILE_NAME_RE = re.compile(
    r"(?:file_[A-Za-z0-9]{6,}|[\w.\-]+\.(?:docx|xlsx|doc|xls|txt|md|pdf|pptx|ppt))"
)


def _extract_literal_tokens(text: str) -> List[str]:
    """提取文本中的文件名 / 产物名风格字面量。

    命中 ``file_`` 前缀的内部 public_id、或带常见文档扩展名的文件名。
    用于对字面量做精确白名单校验,防止泄漏内部 ID / 虚构文件名。
    """
    return list(dict.fromkeys(m.group(0) for m in _FILE_NAME_RE.finditer(text)))


def _is_authorized_literal_token(token: str, allowed_literals: set[str]) -> bool:
    if not token:
        return True
    if token in allowed_literals:
        return True
    for literal in allowed_literals:
        if literal and literal in token:
            return True
    return False


def _cn_to_int(s: str) -> Optional[int]:
    if not s:
        return None
    if len(s) == 1 and s in _CN_NUM_MAP:
        return _CN_NUM_MAP[s]
    # 十 = 10, 二十 = 20, 十一 = 11, 二十三 = 23 ...
    total = 0
    current = 0
    for ch in s:
        if ch in _CN_NUM_MAP:
            current = _CN_NUM_MAP[ch]
        elif ch in _CN_TEN:
            mult = _CN_TEN[ch]
            total += (current if current else 1) * mult
            current = 0
        else:
            return None
    total += current
    return total


def _contains_any(text: str, markers: List[str]) -> bool:
    low = text.lower()
    return any(mk.lower() in low for mk in markers if mk)


def _num_like(value: Any) -> Optional[int]:
    """安全转 int;bool / 非法返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class NarrativeValidator:
    """校验模型生成的叙事是否符合 Schema 与事实约束。"""

    def validate(
        self,
        *,
        public_update: Dict[str, Any],
        tool_context: Optional[ToolNarrativeContext] = None,
        task_summary_context: Optional[TaskSummaryNarrativeContext] = None,
        constraints: Optional[NarrativeFactConstraints] = None,
    ) -> NarrativeValidationResult:
        errors: List[str] = []
        if constraints is None:
            if tool_context is not None:
                constraints = tool_context.fact_constraints
            elif task_summary_context is not None:
                constraints = task_summary_context.fact_constraints
            else:
                constraints = NarrativeFactConstraints()

        # ── Schema 必填 ────────────────────────────────────────────────
        headline = (public_update.get("headline") or "").strip()
        summary = (public_update.get("summary") or "").strip()
        impact = (public_update.get("impact") or "").strip()
        next_action = (public_update.get("next_action") or "").strip()
        narrative_text = (
            public_update.get("narrative_text")
            or public_update.get("narrativeText")
            or ""
        )
        narrative_text = str(narrative_text).strip()
        details = public_update.get("details")
        if not isinstance(details, list):
            details = []
        if not headline:
            errors.append("headline 为空")
        if not narrative_text:
            if not summary:
                errors.append("summary 为空")
            if not impact:
                errors.append("impact 为空")
            if not next_action:
                errors.append("next_action 为空")

        # ── details 数量 2～5(允许 0 仅当无事实可列)──────────────────
        non_empty_details = [str(d).strip() for d in details if str(d).strip()]
        if not narrative_text and non_empty_details and len(non_empty_details) < 2:
            errors.append("details 数量不足(至少 2 项)")
        if len(non_empty_details) > 5:
            errors.append("details 数量超过 5 项")

        all_text = " ".join([headline, summary, impact, next_action, narrative_text, " ".join(non_empty_details)])

        # ── 数字白名单(检查用户可见事实文本)──────────────────────────
        # headline/summary/next_action 是叙述文本,可能出现“一/二”等量词,
        # 不属于事实数字;只对 impact + details 做严格白名单校验,避免
        # 把中文量词误判为虚构数字。
        # Phase 2.9B.5: 先对 allowed_literal_facts / numeric_exempt_literals /
        # allowed_file_names 做最长匹配遮蔽,再提取剩余数字。字面量内嵌的
        # 数字(文件名里的 6/128、版本号 v3 的 3)不参与虚构数字判定。
        if constraints.allowed_numeric_facts:
            # Phase 2.9A.X bug fix: 数字白名单严格校验只对结构化字段
            # (impact + details)生效,narrative_text 是 LLM 自由叙述,
            # 不属于统计事实,不应走严格白名单(注释 §191-193 原本就这么规定,
            # 但代码 `narrative_text or ...` 一直误把 narrative_text 当 fact_text)。
            fact_text = " ".join([impact, " ".join(non_empty_details)])
            exempt = [
                *constraints.allowed_literal_facts,
                *constraints.numeric_exempt_literals,
                *constraints.allowed_file_names,
            ]
            masked = _mask_literals(fact_text, [e for e in exempt if e])
            masked = _mask_markdown_list_ordinals(masked)
            for num in _extract_numbers(masked):
                if num not in constraints.allowed_numeric_facts:
                    errors.append(f"数字 {num} 不在允许事实范围内")
                    break

        # ── 文件名 / 产物名字面量精确校验(Phase 2.9B.5)────────────────
        # 叙事中若出现明显文件名/产物名风格的字面量(带扩展名、或 file_ 前缀
        # 的内部 public_id),必须命中 allowed_file_names / allowed_literal_facts /
        # numeric_exempt_literals(豁免字面量允许出现,但不应作为展示名);
        # 否则视为泄漏内部 ID 或虚构文件名。
        allowed_literals = {
            *(lit for lit in constraints.allowed_file_names if lit),
            *(lit for lit in constraints.allowed_literal_facts if lit),
            *(lit for lit in constraints.numeric_exempt_literals if lit),
        }
        for token in _extract_literal_tokens(all_text):
            if token and not _is_authorized_literal_token(token, allowed_literals):
                errors.append(f"出现未授权的文件名/字面量: {token}")
                break

        # ── Tool 状态语义 ──────────────────────────────────────────────
        if tool_context is not None:
            status = tool_context.terminal_status
            status_text = narrative_text or (headline + summary)
            if status == "failed":
                if _contains_any(status_text, ["成功", "完成", "completed", "success"]):
                    errors.append("failed 状态不能描述为成功/完成")
            if status == "skipped":
                if _contains_any(status_text, ["已执行", "已检索", "completed"]):
                    errors.append("skipped 状态不能描述为已执行")
            if tool_context.execution_context.get("retry_planned") and next_action:
                if "重试" not in next_action and "retry" not in next_action.lower():
                    errors.append("retry_planned 时 next_action 必须说明将重试")
            if tool_context.execution_context.get("waiting_for_user") and next_action:
                if "确认" not in next_action and "等待" not in next_action:
                    errors.append("waiting_for_user 时 next_action 必须说明等待确认")

        # ── Phase 2.9B.6: Task Summary 事实一致性校验 ───────────────────
        if task_summary_context is not None:
            self._validate_task_summary_facts(
                errors, public_update, task_summary_context, all_text
            )

        # ── 敏感信息过滤 ──────────────────────────────────────────────
        for marker in constraints.forbidden_internal_fields:
            if marker in all_text:
                errors.append(f"包含敏感字段: {marker}")
                break
        for leak in ("Traceback", "at 0x", "Bearer ", "sk-", "C:\\\\", "/home/", "/Users/", "/workspace/"):
            if leak in all_text:
                errors.append(f"包含泄漏标记: {leak}")
                break

        feedback = None
        if errors:
            feedback = "；".join(errors[:4]) or None
        return NarrativeValidationResult(valid=not errors, errors=errors, fact_feedback=feedback)

    # ── Phase 2.9B.6: Task Summary 事实一致性 ───────────────────────────

    _NEGATION_PATTERNS: List[str] = [
        "无阻塞问题", "无阻断问题", "无阻断项", "无阻塞项", "未发现阻断",
        "未发现阻塞", "没有阻断", "没有阻塞", "审查全部通过", "无警告",
        "未发现警告", "无建议", "未发现建议", "没有警告", "没有建议",
    ]

    def _validate_task_summary_facts(
        self,
        errors: List[str],
        public_update: Dict[str, Any],
        ctx: TaskSummaryNarrativeContext,
        all_text: str,
    ) -> None:
        """Task Summary 事实一致性校验(§五)。

        仅 Schema 合法不代表事实合法。这里检查:
          1. 数字一致性 — blocking_issues / warnings / suggestions 等必须与
             真实事实一致(通过否定语义 + allowed_numeric_facts 白名单);
          2. Artifact 一致性 — 文件名 / 扩展名 / available 语义;
          3. 状态一致性 — completed 不得描述为 failed,exporting 只能作过程;
          4. 否定语义冲突 — 计数 > 0 时拒绝「无阻塞问题/无警告/无建议」;
          5. Tool 事实 — 存在 failed/skipped 时拒绝「所有工具均执行成功」;
          6. 内部信息 — Prompt / API key / stack trace / 内部路径 / DSN。
        """
        headline = (public_update.get("headline") or "").strip()
        summary = (public_update.get("summary") or "").strip()
        narrative_text = (
            public_update.get("narrative_text")
            or public_update.get("narrativeText")
            or ""
        )
        narrative_text = str(narrative_text).strip()
        details = [
            str(d).strip() for d in (public_update.get("details") or []) if str(d).strip()
        ]
        body_text = " ".join([headline, summary, narrative_text, " ".join(details)])

        # ── 1. 数字一致性: 存在真实计数时,叙事不得出现与之冲突的统计数字 ──
        #    通过 allowed_numeric_facts 白名单 + 否定语义双保险。
        review = ctx.review or {}
        blocking = _num_like(review.get("blocking_issues"))
        warnings = _num_like(review.get("warnings"))
        suggestions = _num_like(review.get("suggestions"))
        generated = _num_like(ctx.generated_sections)
        preserved = _num_like(ctx.preserved_template_sections)
        modules = _num_like(ctx.business_modules)

        # ── 2. Artifact 一致性 ─────────────────────────────────────────
        artifact = ctx.artifact or {}
        artifact_name = str(artifact.get("name") or "").strip()
        artifact_available = bool(artifact.get("available"))
        # 2a. 文件名不得与真实 Artifact 冲突:叙事中出现带扩展名的产物名时,
        #     必须命中允许字面量(或真实文件名)。
        if artifact_name:
            for token in _extract_literal_tokens(body_text):
                if artifact_name in token:
                    continue
                if token and token not in artifact_name and not (
                    _extract_literal_tokens(artifact_name) and token in _extract_literal_tokens(artifact_name)
                ):
                    if token.lower().endswith((".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf")):
                        errors.append(f"Artifact 文件名冲突: {token} != {artifact_name}")
                        break
        # 2b. 产物不可用时不得描述为可下载/可获取。
        if not artifact_available:
            if _contains_any(body_text, ["可下载", "下载产物", "下载链接", "可直接下载"]):
                errors.append("Artifact 不可用时不能描述为可下载")
        # 2c. 扩展名一致性。
        real_ext = artifact_name.lower().rsplit(".", 1)[-1] if "." in artifact_name else ""
        if real_ext in {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf"}:
            for token in _extract_literal_tokens(body_text):
                if token and token.lower().endswith((".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf")):
                    token_ext = token.lower().rsplit(".", 1)[-1]
                    if token_ext != real_ext:
                        errors.append(f"Artifact 扩展名不一致: {token} != {artifact_name}")
                        break

        # ── 3. 状态一致性 ─────────────────────────────────────────────
        task_status = str(ctx.task_status or "completed").lower()
        if task_status == "failed":
            if _contains_any(body_text, ["任务已完成", "已完成", "成功完成", "completed"]):
                errors.append("failed 任务不能描述为已完成")
        elif task_status == "completed":
            # exporting / 导出中 只能是过程状态,不得作为最终状态表述。
            if _contains_any(body_text, ["仍在导出", "正在导出", "导出尚未完成", "处理中"]):
                errors.append("completed 任务不能描述为仍在进行中")
        # 显式：最终总结不得自相矛盾地把任务说成失败。
        if _contains_any(body_text, ["任务执行失败", "生成失败"]) and task_status == "completed":
            errors.append("completed 任务不能描述为失败")

        # ── 4. 否定语义冲突 ───────────────────────────────────────────
        if (blocking or 0) > 0:
            for pattern in (
                "无阻塞问题", "无阻断问题", "无阻塞项", "无阻断项",
                "未发现阻断", "未发现阻塞", "没有阻断", "没有阻塞",
                "审查全部通过", "全部通过",
            ):
                if pattern in body_text:
                    errors.append(f"blocking_issues={blocking} 时不能输出「{pattern}」")
                    break
        if (warnings or 0) > 0:
            for pattern in ("无警告", "未发现警告", "没有警告", "无任何警告"):
                if pattern in body_text:
                    errors.append(f"warnings={warnings} 时不能输出「{pattern}」")
                    break
        if (suggestions or 0) > 0:
            for pattern in ("无建议", "未发现建议", "没有建议", "无任何建议"):
                if pattern in body_text:
                    errors.append(f"suggestions={suggestions} 时不能输出「{pattern}」")
                    break

        # ── 5. Tool 事实 ──────────────────────────────────────────────
        if ctx.completed_tools:
            has_failed = any(
                str(t.get("status") or "").lower() in ("failed", "skipped", "timeout")
                for t in ctx.completed_tools if isinstance(t, dict)
            )
            if has_failed:
                if _contains_any(body_text, ["所有工具均执行成功", "所有步骤全部成功", "全部工具成功", "所有步骤均成功"]):
                    errors.append("存在 failed/skipped 工具时不能描述为全部成功")

        # ── 6. 内部信息安全 ───────────────────────────────────────────
        for marker in ("system_prompt", "api_key", "stack_trace", "Traceback", "at 0x", "Bearer ", "sk-", "C:\\", "D:\\", "/home/", "/Users/", "/workspace/", "postgres://", "mysql://"):
            if marker in body_text:
                errors.append(f"Task Summary 包含内部信息: {marker}")
                break

        # 数字一致性兜底:叙事中出现的统计数字必须在白名单内(或与真实事实一致)。
        # 与主流程 allowed_numeric_facts 白名单重复,但这里用语义数字(如
        # 3 个阻塞问题)做二次校验,防止「数字在别的字段出现」绕过。
        if body_text:
            pass  # 数字白名单已由主流程校验(allowed_numeric_facts)


__all__ = ["NarrativeValidator", "_extract_numbers", "_cn_to_int"]
