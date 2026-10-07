"""F020 — VisionService.

Thin wrapper around :class:`app.integrations.llm_client.LLMClient`
configured for the per-user image-understanding endpoint.  Re-uses
the existing OpenAI-compatible multimodal call path (``images`` arg)
so no new HTTP client is introduced.

The vision model is asked to return a strict JSON document describing
the image; :meth:`VisionService._parse_to_vision_result` normalises
the raw response into :class:`VisionResult`.  F020 deliberately
skips quality scoring, retry, and caching — these are deferred to
F021 per the plan.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.integrations.llm_client import LLMClient, LLMClientError
from app.llm.task_profiles import LLMParserType, LLMTaskProfile

logger = logging.getLogger(__name__)

VISION_EVIDENCE_CONTRACT_VERSION = "vision_evidence:v1"


# ── Vision result ───────────────────────────────────────────────


@dataclass
class VisionResult:
    """Structured interpretation of a single requirement-document image.

    Mirrors the legacy PlanWise QA output contract: image_type, a
    short business-meaning summary, visible text, business flow,
    UI elements, data fields, test points, and free-form uncertainty
    notes.  Optional ``error`` is populated when the model call
    succeeded but the response could not be parsed.
    """

    image_type: str = ""
    summary: str = ""
    visible_text: str = ""
    business_flow: str = ""
    ui_elements: list[str] = field(default_factory=list)
    data_fields: list[str] = field(default_factory=list)
    test_points: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    error: str = ""
    # Multi-modal input is evidence, not a text-context snapshot.  Keep only
    # metadata here; raw pixels/OCR text remain in the attachment pipeline.
    audit_metadata: dict[str, str | int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error

    def to_prompt_text(self, image_index: int, source: str = "") -> str:
        """Render the structured result as a prompt-insertion block."""
        if not self.ok:
            return (
                f"【图片 {image_index} 理解失败】来源位置：{source or '需求文档'}\n"
                f"原因：{self.error}"
            )

        lines: list[str] = [
            f"【图片 {image_index} 理解结果】",
            f"图片类型：{self.image_type or '未识别'}",
            f"来源位置：{source or '需求文档'}",
            f"业务含义摘要：{self.summary or '无'}",
            f"OCR 识别文字：{self.visible_text or '无'}",
            f"业务流程：{self.business_flow or '无'}",
        ]
        if self.ui_elements:
            lines.append("页面元素：" + "；".join(self.ui_elements))
        if self.data_fields:
            lines.append("关键字段：" + "；".join(self.data_fields))
        if self.test_points:
            lines.append("测试点：" + "；".join(self.test_points))
        if self.uncertainties:
            lines.append("不确定信息：" + "；".join(self.uncertainties))
        return "\n".join(lines)


# ── Vision service ──────────────────────────────────────────────


VISION_SYSTEM_PROMPT = (
    "你是需求文档图片理解助手。请仔细分析用户上传的图片，"
    "判断它属于哪种类型（流程图 / 状态机 / ER 图 / UI 原型 / 表格截图 / 业务架构图 / 其他），"
    "提取其中的业务含义、关键流程、UI 元素、数据字段，"
    "并给出该图片暗示的测试要点。"
    "必须只输出一个合法的 JSON 对象，禁止任何解释、Markdown 代码块或多余文字。"
)


_VISION_USER_PROMPT = """图片编号：{image_index}
图片来源：{source}

OCR 预提取文字（如有）：
{ocr_hint}

请基于图片内容输出一个 JSON 对象，结构如下：
{{
  "image_type": "流程图 | 状态机 | ER 图 | UI 原型 | 表格截图 | 业务架构图 | 其他",
  "summary": "30字以内的业务含义摘要",
  "visible_text": "图片中清晰可读的文字",
  "business_flow": "描述图中的流程走向（流程图/状态机/架构图必填）",
  "ui_elements": ["页面元素1", "页面元素2"],
  "data_fields": ["字段1", "字段2"],
  "test_points": ["测试点1", "测试点2"],
  "uncertainties": ["不确定信息1"]
}}
""".strip()


VISION_ANALYSIS_PROFILE = LLMTaskProfile(
    name="vision.image_analysis",
    system_prompt=VISION_SYSTEM_PROMPT,
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=1600,
    temperature=0.0,
)


class VisionService:
    """Multimodal LLM call wrapper for image understanding."""

    def __init__(
        self,
        llm_config: Any,
        *,
        context_llm_invoker: Any | None = None,
        user_id: int | None = None,
        session_factory: Any | None = None,
        conversation_id: int | None = None,
        runtime_context: Any | None = None,
    ) -> None:
        # ``llm_config`` is an ``ImageUnderstandingConfigProvider``; the
        # LLMClient only needs api_url / api_key / model_name / timeout.
        self._client = LLMClient(config_provider=llm_config)
        self._context_llm_invoker = context_llm_invoker
        self._user_id = user_id
        self._session_factory = session_factory
        self._conversation_id = conversation_id
        self._runtime_context = runtime_context

    async def analyze_image(
        self,
        image_path: Path,
        source: str,
        ocr_text: str,
        image_index: int,
    ) -> VisionResult:
        """Call the vision model and parse the JSON response.

        Returns a :class:`VisionResult` (with ``error`` populated on
        parse failure).  Does NOT raise on LLM errors — those are
        converted into :class:`VisionResult.error` so the orchestrator
        can degrade gracefully to OCR-only.
        """
        ocr_hint = (ocr_text or "").strip() or "（无 OCR 文字）"
        user_prompt = _VISION_USER_PROMPT.format(
            image_index=image_index,
            source=source or "需求文档",
            ocr_hint=ocr_hint,
        )
        audit_metadata: dict[str, str | int] = {
            "evidence_contract_version": VISION_EVIDENCE_CONTRACT_VERSION,
            "input_kind": "image",
            "image_index": image_index,
            "ocr_char_count": len(ocr_text or ""),
            "snapshot_mode": "not_applicable",
        }
        logger.info(
            "EXTERNAL_MODEL_BOUNDARY | component=vision_service | "
            "operation=analyze_image | contract=%s | image_index=%s | ocr_chars=%s",
            VISION_EVIDENCE_CONTRACT_VERSION,
            image_index,
            len(ocr_text or ""),
        )
        try:
            bridge = self._context_llm_invoker
            if bridge is None or not getattr(bridge, "available", False):
                raise RuntimeError("context_engine_unavailable")
            from types import SimpleNamespace

            runtime_context = self._runtime_context or SimpleNamespace(
                session_factory=self._session_factory,
                llm_client=self._client,
                user_internal_id=self._user_id,
                conversation_internal_id=self._conversation_id,
            )
            result = await bridge.generate(
                user_id=int(self._user_id or 0),
                conversation_id=self._conversation_id,
                call_site="vision.image_analysis",
                llm_task_profile=VISION_ANALYSIS_PROFILE,
                current_goal=user_prompt,
                user_content=user_prompt,
                output_contract="json",
                image_paths=[str(image_path)],
                runtime_context=runtime_context,
            )
            value = getattr(result, "value", None) if result is not None else None
            if not isinstance(value, dict):
                raise RuntimeError("vision_profile_parse_failed")
            raw = json.dumps(value, ensure_ascii=False)
        except LLMClientError as exc:
            logger.warning(
                "VisionService.analyze_image: 视觉模型调用失败 | "
                "image_index=%d | err=%s", image_index, exc,
            )
            return VisionResult(
                error=f"vision_model_failed: {exc}",
                audit_metadata={**audit_metadata, "status": "failed"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "VisionService.analyze_image: Context Engine invocation failed | image_index=%d | err=%s",
                image_index,
                str(exc)[:200],
            )
            return VisionResult(
                error=f"vision_context_engine_failed: {exc}",
                audit_metadata={**audit_metadata, "status": "failed"},
            )

        return self._parse_to_vision_result(
            raw,
            image_index,
            source,
            audit_metadata={**audit_metadata, "status": "completed"},
        )

    @staticmethod
    def _parse_to_vision_result(
        raw: str,
        image_index: int,
        source: str,
        *,
        audit_metadata: dict[str, str | int] | None = None,
    ) -> VisionResult:
        """Parse the raw LLM text into a :class:`VisionResult`.

        Tries (in order): strict ``json.loads`` → regex ``{...}``
        fallback → error envelope.  Never raises.
        """
        text = (raw or "").strip()
        if not text:
            return VisionResult(
                error="empty response from vision model",
                audit_metadata=dict(audit_metadata or {}),
            )

        parsed: Optional[dict[str, Any]] = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except json.JSONDecodeError:
                    parsed = None

        if not isinstance(parsed, dict):
            logger.warning(
                "VisionService: 模型返回不是合法 JSON | image_index=%d | preview=%s",
                image_index, text[:200],
            )
            return VisionResult(
                error=f"vision response is not valid JSON: {text[:120]}",
                audit_metadata=dict(audit_metadata or {}),
            )

        def _list_of_str(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(item) for item in value if str(item).strip()]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        return VisionResult(
            image_type=str(parsed.get("image_type") or ""),
            summary=str(parsed.get("summary") or ""),
            visible_text=str(parsed.get("visible_text") or ""),
            business_flow=str(parsed.get("business_flow") or ""),
            ui_elements=_list_of_str(parsed.get("ui_elements")),
            data_fields=_list_of_str(parsed.get("data_fields")),
            test_points=_list_of_str(parsed.get("test_points")),
            uncertainties=_list_of_str(parsed.get("uncertainties")),
            audit_metadata=dict(audit_metadata or {}),
        )

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Vision Model 调用层):
#
#   链路:
#     ImageUnderstandingOrchestrator 同步调用 → VisionService.understand(image_bytes)
#       → 调用底层 vision_provider(multi_modal_llm / 三方 API)
#       → 返回 structured caption + tags + ocr_overlay(Vision 模型的输出)
#     然后 ImageBlockResult 拼接到 RequirementParserTool 的 text_content 里
#
# 关键约束(供开发者速查):
#   - Vision Provider 是 per-user 配置(image_understanding_configs 表);
#   - 单张图超时 30s(由 feature_flags 控制);
#   - Vision provider 不可用 → service 直接 raise(上层 Orchestrator 兜底);
#   - 不在本服务缓存(由 ImageUnderstandingOrchestrator 决定是否缓存)。
