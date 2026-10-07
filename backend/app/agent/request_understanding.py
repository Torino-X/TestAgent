"""Request understanding layer for capability routing.

This module sits after the legacy intent router.  It translates an
intent/route guess plus deterministic message facts into a capability-
oriented request shape.  The result is intentionally conservative:
unknown or degraded turns fall back to ordinary chat, never to task
creation.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.enums import IntentType, MessageRoute
from app.agent.intent_router import IntentResult


class RequestClass(StrEnum):
    CHAT = "chat"
    AGENT_TASK = "agent_task"
    TASK_ACTION = "task_action"
    CLARIFICATION = "clarification"


class RequestUnderstandingResult(BaseModel):
    request_class: RequestClass
    operation: str = "qa"
    target_capability: str = "general_chat"
    task_type: str | None = None
    active_task_action: str | None = None
    attachment_context: bool = False
    attachment_file_exts: list[str] = Field(default_factory=list)
    vision_required: bool = False
    knowledge_scope_hint: str = "general"
    enterprise_knowledge_likelihood: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    degraded: bool = False
    reason: str = ""


_GENERATE_WORDS = (
    "生成",
    "编写",
    "创建",
    "输出",
    "制定",
    "generate",
    "create",
    "write",
)
_NEGATED_GENERATE_PATTERNS = (
    "不要生成",
    "不生成",
    "无需生成",
    "不用生成",
    "暂不生成",
    "do not generate",
    "don't generate",
    "no need to generate",
)
_TEST_PLAN_WORDS = ("测试方案", "测试计划", "测试文档", "test plan", "test-plan", "testplan")
_TEST_CASE_WORDS = ("测试用例", "test case", "testcase")
_QUESTION_WORDS = ("是什么", "什么是", "解释", "说明", "介绍", "怎么", "如何", "?")
_DOCUMENT_WORDS = ("文档", "附件", "文件", "需求", "prd", "模板", "总结", "提取")
_SUMMARIZE_WORDS = ("总结", "概括", "summarize", "summary")
_ANALYZE_WORDS = ("分析", "analyze", "analysis")
_EXTRACT_WORDS = ("提取", "抽取", "extract")
_COMPARE_WORDS = ("比较", "对比", "compare")
_ENTERPRISE_WORDS = (
    "公司",
    "知识库",
    "内部",
    "规范",
    "制度",
    "历史项目",
    "maas",
    "企业",
)
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def build_request_understanding(
    content: str,
    *,
    intent_result: IntentResult | None = None,
    files: list[Any] | None = None,
    attached_file_ids: list[str] | None = None,
    intent_context: Any | None = None,
    degraded: bool = False,
    reason: str = "",
) -> RequestUnderstandingResult:
    text = (content or "").strip()
    lowered = text.lower()
    compact = "".join(lowered.split())
    attached_ids = list(attached_file_ids or [])
    attachment_context = bool(attached_ids)
    if files:
        attachment_context = attachment_context or bool(files)
    attachment_file_exts = _attached_file_exts(files, attached_ids)

    if _is_existing_task_action(intent_result, intent_context, compact):
        return RequestUnderstandingResult(
            request_class=RequestClass.TASK_ACTION,
            operation="continue",
            target_capability="active_task_action",
            active_task_action="continue_or_confirm",
            confidence=max(_confidence(intent_result), 0.9),
            degraded=degraded,
            reason=reason or "existing_task_action",
        )

    if degraded or not text:
        return RequestUnderstandingResult(
            request_class=RequestClass.CHAT,
            target_capability="general_chat",
            confidence=0.2,
            degraded=True,
            reason=reason or "degraded_fallback_to_chat",
        )

    if _is_test_plan_generation(intent_result, compact, lowered):
        return RequestUnderstandingResult(
            request_class=RequestClass.AGENT_TASK,
            operation="generate",
            target_capability="test_plan_generation",
            task_type="test_plan_generation",
            attachment_context=attachment_context,
            attachment_file_exts=attachment_file_exts,
            confidence=max(_confidence(intent_result), 0.9),
            reason="test_plan_generation",
        )

    if _is_test_case_generation(intent_result, compact, lowered):
        return RequestUnderstandingResult(
            request_class=RequestClass.AGENT_TASK,
            operation="generate",
            target_capability="test_case_generation",
            task_type="test_case_generation",
            attachment_context=attachment_context,
            attachment_file_exts=attachment_file_exts,
            confidence=max(_confidence(intent_result), 0.75),
            reason="test_case_generation",
        )

    if _is_result_modification(intent_result):
        return RequestUnderstandingResult(
            request_class=RequestClass.AGENT_TASK,
            operation="modify",
            target_capability="test_plan_incremental",
            task_type="incremental_test_plan",
            attachment_context=attachment_context,
            attachment_file_exts=attachment_file_exts,
            confidence=max(_confidence(intent_result), 0.8),
            reason="result_modification",
        )

    if _is_concept_question(compact, lowered):
        return RequestUnderstandingResult(
            request_class=RequestClass.CHAT,
            target_capability="general_chat",
            confidence=max(_confidence(intent_result), 0.8),
            reason="concept_question",
        )

    vision_required = _vision_required(files, attached_ids)
    if vision_required:
        return RequestUnderstandingResult(
            request_class=RequestClass.CHAT,
            target_capability="image_qa",
            attachment_context=True,
            attachment_file_exts=attachment_file_exts,
            vision_required=True,
            confidence=max(_confidence(intent_result), 0.8),
            reason="image_attachment_question",
        )

    if _is_document_question(intent_result, compact, attachment_context):
        return RequestUnderstandingResult(
            request_class=RequestClass.CHAT,
            operation=_document_operation(compact),
            target_capability="document_qa",
            attachment_context=True,
            attachment_file_exts=attachment_file_exts,
            confidence=max(_confidence(intent_result), 0.8),
            reason="document_question",
        )

    enterprise_likelihood = _enterprise_likelihood(intent_result, compact)
    if enterprise_likelihood >= 0.6:
        return RequestUnderstandingResult(
            request_class=RequestClass.CHAT,
            target_capability="enterprise_knowledge_qa",
            knowledge_scope_hint="enterprise",
            enterprise_knowledge_likelihood=enterprise_likelihood,
            confidence=max(_confidence(intent_result), 0.8),
            reason="enterprise_knowledge_question",
        )

    return RequestUnderstandingResult(
        request_class=RequestClass.CHAT,
        target_capability="general_chat",
        confidence=max(_confidence(intent_result), 0.7),
        reason="general_chat",
    )


def _confidence(intent_result: IntentResult | None) -> float:
    if intent_result is None:
        return 0.0
    try:
        return float(intent_result.confidence)
    except (TypeError, ValueError):
        return 0.0


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word.lower() in text for word in words)


def _is_existing_task_action(
    intent_result: IntentResult | None,
    intent_context: Any | None,
    compact: str,
) -> bool:
    if intent_result is not None and intent_result.route == MessageRoute.EXISTING_TASK_ACTION:
        return True
    latest = getattr(intent_context, "latest_task_summary", None)
    status = getattr(latest, "status", "")
    if status in {"waiting_user_confirm", "running", "generating", "reviewing", "exporting"}:
        return any(word in compact for word in ("确认", "继续", "开始", "按这个", "确认章节策略"))
    return False


def _is_test_plan_generation(
    intent_result: IntentResult | None,
    compact: str,
    lowered: str,
) -> bool:
    if intent_result is not None and intent_result.intent == IntentType.TEST_PLAN_GENERATION:
        return True
    return _contains_any(compact, _GENERATE_WORDS) and (
        _contains_any(compact, _TEST_PLAN_WORDS) or _contains_any(lowered, _TEST_PLAN_WORDS)
    )


def _is_test_case_generation(
    intent_result: IntentResult | None,
    compact: str,
    lowered: str,
) -> bool:
    if _contains_any(lowered, _NEGATED_GENERATE_PATTERNS):
        return False
    if intent_result is not None and intent_result.intent == IntentType.TEST_CASE_GENERATION:
        return _contains_any(compact, _GENERATE_WORDS)
    return _contains_any(compact, _GENERATE_WORDS) and (
        _contains_any(compact, _TEST_CASE_WORDS) or _contains_any(lowered, _TEST_CASE_WORDS)
    )


def _is_result_modification(intent_result: IntentResult | None) -> bool:
    if intent_result is None:
        return False
    return (
        intent_result.intent == IntentType.RESULT_MODIFICATION
        and intent_result.route == MessageRoute.AGENT_TASK
    )


def _is_concept_question(compact: str, lowered: str) -> bool:
    mentions_test_case = _contains_any(compact, _TEST_CASE_WORDS) or _contains_any(
        lowered, _TEST_CASE_WORDS
    )
    mentions_test_plan = _contains_any(compact, _TEST_PLAN_WORDS) or _contains_any(
        lowered, _TEST_PLAN_WORDS
    )
    return (mentions_test_case or mentions_test_plan) and _contains_any(compact, _QUESTION_WORDS)


def _is_document_question(
    intent_result: IntentResult | None,
    compact: str,
    attachment_context: bool,
) -> bool:
    if intent_result is not None and intent_result.intent == IntentType.DOCUMENT_QUESTION:
        return True
    return attachment_context and _contains_any(compact, _DOCUMENT_WORDS)


def _document_operation(compact: str) -> str:
    if _contains_any(compact, _COMPARE_WORDS):
        return "compare"
    if _contains_any(compact, _EXTRACT_WORDS):
        return "extract"
    if _contains_any(compact, _ANALYZE_WORDS):
        return "analyze"
    if _contains_any(compact, _SUMMARIZE_WORDS):
        return "summarize"
    return "qa"


def _enterprise_likelihood(intent_result: IntentResult | None, compact: str) -> float:
    if intent_result is not None and intent_result.intent == IntentType.KNOWLEDGE_QUESTION:
        return 0.9
    hits = sum(1 for word in _ENTERPRISE_WORDS if word in compact)
    if hits >= 2:
        return 0.9
    if hits == 1:
        return 0.7
    return 0.0


def _vision_required(files: list[Any] | None, attached_ids: list[str]) -> bool:
    if not files:
        return False
    attached = set(attached_ids)
    for file in files:
        public_id = getattr(file, "public_id", None)
        if attached and public_id not in attached:
            continue
        ext = _normalized_file_ext(file)
        mime = str(getattr(file, "mime_type", "") or "").lower()
        if ext in _IMAGE_EXTENSIONS or mime.startswith("image/"):
            return True
    return False


def _attached_file_exts(files: list[Any] | None, attached_ids: list[str]) -> list[str]:
    if not files:
        return []
    attached = set(attached_ids)
    exts: list[str] = []
    for file in files:
        public_id = getattr(file, "public_id", None)
        if attached and public_id not in attached:
            continue
        ext = _normalized_file_ext(file)
        if ext and ext not in exts:
            exts.append(ext)
    return exts


def _normalized_file_ext(file: Any) -> str:
    ext = str(getattr(file, "file_ext", "") or "").strip().lower()
    if ext:
        return ext if ext.startswith(".") else f".{ext}"

    original_name = str(getattr(file, "original_name", "") or "").strip()
    suffix = Path(original_name).suffix.lower()
    if suffix:
        return suffix

    mime = str(getattr(file, "mime_type", "") or "").strip().lower()
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return ".docx"
    return ""


# 模块定位:请求理解层(Phase 2.8C,capability 路由的前置)
#
# 取代旧 intent_router 的 LLM-based classification,改用基于模板 + 关键词
# + LLM 协同的轻量分类,把意图降级为 capability(语义级)。
#
# 链路:
#   IntentRouter / MessageService
#     → RequestUnderstanding.understand(prompt, attachments)
#       → RequestUnderstandingResult(capability=…, confidence=…, params=…)
#     → CapabilityRouter.route(result)
#
# 关键约束:
#   - 不直接调 LLM(用 rule-based + light classifier);
#   - 不写 DB(纯函数);
#   - 升级到 LangGraph 主图后,本模块可退役,但当前 Phase 2.9A 仍活跃。
