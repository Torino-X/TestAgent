"""Summary Facts — Phase 2.9A.20 单一事实构造器。

集中所有摘要字段的事实源,供:

  * ``generate_completion_summary_node._build_summary_facts``(LLM 提示词)
  * ``finalize_task_node.task_completed`` payload(前端事件流)
  * ``events-post-confirm`` 历史回放的事件聚合
  * ``AgentTaskService._to_detail`` 任务详情接口

统一语义(技术方案 §6.2):
  * 章节数 = ``test_plan_content.generated_sections`` (len(list) 或 int)
  * 业务模块数 = ``requirement_analysis.modules`` (real field, 仅 list) 或
                  ``requirement_analysis.business_modules``(旧字段兼容)
  * 审查数 = ``review_result.{issues / suggestions / warnings}`` 三档分级
  * Artifact 状态 = ``artifact`` + ``format_check_result.status`` 二元合成

设计约束:
  * 只读 state,不修改,不依赖 LLM;
  * 空 State 安全回退(返回 ``{}`` 而非抛错);
  * 实时 SSE / 历史回放共用同一函数,确保一致;
  * 不爬错误路径(任务失败时仍返回已成功累计的事实,而不是清零)。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _count_chapters(test_plan_content: Any) -> int:
    """章节数 — Phase 2.9A.11 规范化结构(len of generated_sections)优先。"""
    if not isinstance(test_plan_content, dict):
        return 0
    generated = test_plan_content.get("generated_sections")
    if isinstance(generated, list):
        return len(generated)
    # 旧 envelope.data 形态:int 字面值
    try:
        return int(generated or 0)
    except (TypeError, ValueError):
        return 0


def _count_kept_sections(test_plan_content: Any) -> int:
    if not isinstance(test_plan_content, dict):
        return 0
    kept = test_plan_content.get("kept_sections")
    if isinstance(kept, list):
        return len(kept)
    try:
        return int(kept or 0)
    except (TypeError, ValueError):
        return 0


def _count_business_modules(requirement_analysis: Any) -> int:
    """业务模块数 — 真实字段路径(不靠章节标题猜测)。

    优先级:
      * ``requirement_analysis.modules`` (规范化字段)
      * ``requirement_analysis.business_modules`` (历史兼容)
    """
    if not isinstance(requirement_analysis, dict):
        return 0
    for key in ("modules", "business_modules"):
        val = requirement_analysis.get(key)
        if isinstance(val, list):
            return len(val)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return 0


def _summarize_review(review_result: Any) -> Dict[str, Any]:
    """Phase 2.9A.20 §6.2 审查分级 — 区分 阻断 / 警告 / 建议 三档。

    输入兼容:
      * review_result.review_issues[].severity in {block, warn}
      * review_result.issues[] (legacy flat list)
      * review_result.suggestions[] (legacy)
      * review_result.warnings[] (legacy)
    """
    summary = {
        "passed": None,
        "level": None,
        "block_count": 0,
        "warning_count": 0,
        "suggestion_count": 0,
        "block_details": [],
        "warning_details": [],
        "suggestion_details": [],
    }
    if not isinstance(review_result, dict):
        return summary

    summary["passed"] = review_result.get("passed")
    summary["level"] = review_result.get("level")
    review_passed = (
        summary["passed"] is True
        or str(summary["level"] or "").lower() == "passed"
    )

    # 优先 review_issues(规范化)
    review_issues = review_result.get("review_issues")
    if isinstance(review_issues, list):
        for it in review_issues:
            if not isinstance(it, dict):
                continue
            severity = str(it.get("severity") or "").lower()
            detail = _review_item_text(it)
            if severity == "block":
                if not review_passed:
                    summary["block_count"] += 1
                    _append_detail(summary["block_details"], detail)
            elif severity in ("warn", "warning"):
                summary["warning_count"] += 1
                _append_detail(summary["warning_details"], detail)
            elif severity == "suggestion":
                summary["suggestion_count"] += 1
                _append_detail(summary["suggestion_details"], detail)
            # severity == "info" 不计入(只记谱面建议,不应升级展示)

    # 兼容旧 envelope 形态。若 review_issues 已存在(list,即使为空),
    # 它就是 3.0 审查事实的权威来源,不能再把 legacy issues 升级为 block。
    if not review_passed and not isinstance(review_issues, list):
        for legacy_key in ("block_issues", "issues"):
            legacy = review_result.get(legacy_key)
            if isinstance(legacy, list):
                summary["block_count"] = max(summary["block_count"], len(legacy))
                for item in legacy:
                    _append_detail(summary["block_details"], _review_item_text(item))

    suggestions = review_result.get("suggestions")
    if isinstance(suggestions, list):
        summary["suggestion_count"] = max(
            summary["suggestion_count"], len(suggestions)
        )
        for item in suggestions:
            _append_detail(summary["suggestion_details"], _review_item_text(item))

    warnings = review_result.get("warnings")
    if isinstance(warnings, list):
        summary["warning_count"] = max(summary["warning_count"], len(warnings))
        for item in warnings:
            _append_detail(summary["warning_details"], _review_item_text(item))

    repair_unresolved = review_result.get("repair_unresolved_issue_details")
    if not review_passed and isinstance(repair_unresolved, list):
        summary["block_count"] = max(summary["block_count"], len(repair_unresolved))
        for item in repair_unresolved:
            _append_detail(summary["block_details"], _review_item_text(item))

    return summary


def _review_item_text(item: Any, limit: int = 160) -> str:
    if isinstance(item, str):
        return item.strip()[:limit]
    if not isinstance(item, dict):
        return ""
    section = item.get("section_id") or item.get("field") or item.get("field_path")
    for key in (
        "message",
        "description",
        "title",
        "summary",
        "content",
        "issue",
        "suggestion",
        "text",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if section:
                section_text = str(section).strip()[:80]
                return f"**{section_text}**: {text}"[:limit]
            return text[:limit]
    return ""


def _append_detail(details: List[str], value: str, limit: int = 8) -> None:
    text = value.strip()
    if not text or text in details or len(details) >= limit:
        return
    details.append(text)


def _summarize_artifact(artifact: Any, format_check_result: Any) -> Dict[str, Any]:
    """Artifact 状态 = artifact 是否存在 + format_check_result.status 二元。"""
    info: Dict[str, Any] = {
        "present": False,
        "public_id": None,
        "file_name": None,
        "page_count": None,
        "format_status": None,
        "format_blocked": False,
    }
    if isinstance(artifact, dict):
        info["present"] = bool(artifact.get("public_id") or artifact.get("storage_path"))
        info["public_id"] = artifact.get("public_id") or artifact.get("artifact_id")
        info["file_name"] = artifact.get("file_name")
        info["page_count"] = _optional_int(artifact.get("page_count") or artifact.get("pageCount"))
    if isinstance(format_check_result, dict):
        status = (
            format_check_result.get("status")
            or format_check_result.get("level")
            or ""
        )
        status = str(status).lower()
        info["format_status"] = status or None
        info["format_blocked"] = status == "blocked"
    return info


def build_summary_facts(state: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 2.9A.20 唯一摘要事实源。

    输入是 Graph State 字典(可能不完整,如部分字段恢复失败的 Checkpoint)。
    返回 canonical dict,供后续 LLM / 事件流 / 任务详情接口共用。

    返回结构:
        {
          "user_instruction": str,
          "generated_sections": int,    # 章节数
          "kept_sections": int,
          "business_modules": int,     # 业务模块数
          "review": {block_count, warning_count, suggestion_count, passed, level},
          "artifact": {present, public_id, file_name, format_status, format_blocked},
          "task_status": str | None,
        }

    强约束:
      * 全字段缺失 / state=None 时返回空 dict 之外的所有 0 / None;
      * 不抛 KeyError;
      * 不修改 state。
    """
    if not isinstance(state, dict):
        return {
            "user_instruction": "",
            "generated_sections": 0,
            "kept_sections": 0,
            "business_modules": 0,
            "review": _summarize_review(None),
            "artifact": _summarize_artifact(None, None),
            "task_status": None,
        }

    return {
        "user_instruction": state.get("user_prompt") or "",
        "generated_sections": _count_chapters(state.get("test_plan_content")),
        "kept_sections": _count_kept_sections(state.get("test_plan_content")),
        "business_modules": _count_business_modules(
            state.get("requirement_analysis")
        ),
        "page_count": (
            _summarize_artifact(
                state.get("artifact"), state.get("format_check_result")
            ).get("page_count")
        ),
        "review": _summarize_review(state.get("review_result")),
        "artifact": _summarize_artifact(
            state.get("artifact"), state.get("format_check_result")
        ),
        "task_status": state.get("task_status"),
    }


def fallback_summary_text(state: Dict[str, Any]) -> str:
    """任务级 fallback 摘要 — 不依赖 LLM,只拼接事实。

    实时 SSE 与历史 event-list 共用,确保一致。
    """
    facts = build_summary_facts(state)
    parts = ["任务已完成。"]
    if facts["generated_sections"] or facts["kept_sections"]:
        parts.append(
            f"已生成 {facts['generated_sections']} 个章节,"
            f"保留 {facts['kept_sections']} 个模板章节,"
            f"业务模块 {facts['business_modules']} 个。"
        )
    review = facts["review"]
    if review["block_count"] or review["warning_count"]:
        parts.append(
            f"审查发现 {review['block_count']} 个阻断问题 "
            f"和 {review['warning_count']} 个警告。"
        )
        if review.get("block_details"):
            parts.append(
                "待关注章节: "
                + "；".join(str(x) for x in review["block_details"][:3])
                + "。"
            )
    artifact = facts["artifact"]
    if artifact["present"]:
        page_note = (
            f"文档页数 {artifact['page_count']} 页。"
            if artifact.get("page_count") is not None
            else ""
        )
        block_note = "格式检查需用户确认。" if artifact["format_blocked"] else ""
        parts.append(
            f"已生成产物: {artifact['file_name'] or '未命名 DOCX'}。{page_note}{block_note}"
        )
    return "".join(parts)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


__all__ = [
    "build_summary_facts",
    "fallback_summary_text",
]


# 模块定位:Phase 2.9A.20 摘要字段的"唯一事实构造器"
#
# 集中 produce_summary_facts(state) 一个函数,供以下三个调用方取**完全一致**
# 的数字事实:
#   * generate_completion_summary_node._build_summary_facts(LLM prompt 用)
#   * task_summary_narrative_node._build_task_summary_context(task summary 用)
#   * finalize_task_node / repair_node(收尾 audit 用)
#
# 链路(所有调用方都先调 build_summary_facts(state),绝不自己 derive):
#   build_summary_facts(state)
#     → review.block_count = review_result.block_issues 里 severity=block
#     → generated / kept_sections / business_modules
#     → artifact.size_bytes / artifact.public_id
#
# 关键约束:
#   - 任何调用方都不允许直接读 review_result.block_count(它不存在),
#     一定走本模块的 build_summary_facts;
#   - Pydantic 模型摘要字段必须 Optional[int],无法 derive 时写 None;
#   - 跨子图(preparation / repair) 都用同一份事实,summary_facts 是 single
#     source of truth;
#   - 失败兜底:返回 minimal set(review=empty,generated=0,artifact empty)。
