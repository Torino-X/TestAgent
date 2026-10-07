"""Atomic capabilities visible to the Dynamic Agent planner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AtomicSideEffect(StrEnum):
    READ = "READ"
    EXTERNAL_READ = "EXTERNAL_READ"
    WRITE = "WRITE"
    EXPORT = "EXPORT"
    DELETE = "DELETE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"


@dataclass(frozen=True, slots=True)
class AtomicCapabilitySpec:
    capability_key: str
    display_name: str
    description_for_planner: str
    executor_kind: str
    backend_target: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    preconditions: list[str]
    postconditions: list[str]
    supported_media_types: list[str]
    side_effect: AtomicSideEffect
    risk_level: str
    timeout_seconds: int
    retry_policy: dict[str, Any]
    planner_visible: bool
    requires_user_confirmation: bool = False


class AtomicCapabilityRegistry:
    def __init__(self, specs: list[AtomicCapabilitySpec]) -> None:
        self._by_key = {spec.capability_key: spec for spec in specs}
        self.validate()

    @classmethod
    def default(cls, *, vision_stable: bool = False) -> "AtomicCapabilityRegistry":
        specs = [
            AtomicCapabilitySpec(
                capability_key="word_document_parse",
                display_name="解析Word文档",
                description_for_planner="Parse an attached Word document into text, structure, tables, and image OCR facts.",
                executor_kind="tool",
                backend_target="RequirementParserTool",
                input_schema={"type": "object", "required": ["file_public_id"]},
                output_schema={"type": "object", "required": ["text_content", "document_structure"]},
                preconditions=["file belongs to current user and conversation", "file extension is .docx"],
                postconditions=["document text and structure are available as evidence"],
                supported_media_types=[".docx"],
                side_effect=AtomicSideEffect.READ,
                risk_level="low",
                timeout_seconds=120,
                retry_policy={"max_attempts": 2},
                planner_visible=True,
            ),
            AtomicCapabilitySpec(
                capability_key="knowledge_search",
                display_name="检索知识库",
                description_for_planner="Search approved knowledge sources according to the RetrievalPlanner policy.",
                executor_kind="tool",
                backend_target="KnowledgeSearchTool",
                input_schema={"type": "object", "required": ["query"]},
                output_schema={"type": "object", "required": ["hits"]},
                preconditions=["retrieval plan allows knowledge search"],
                postconditions=["knowledge evidence is available or no-hit is explicit"],
                supported_media_types=[],
                side_effect=AtomicSideEffect.EXTERNAL_READ,
                risk_level="medium",
                timeout_seconds=60,
                retry_policy={"max_attempts": 2},
                planner_visible=True,
            ),
            AtomicCapabilitySpec(
                capability_key="evidence_analysis",
                display_name="分析证据",
                description_for_planner="Analyze, summarize, compare, or extract facts from verified evidence through Context Engine.",
                executor_kind="llm",
                backend_target="ContextAwareLLMInvoker",
                input_schema={"type": "object", "required": ["operation", "evidence_refs"]},
                output_schema={"type": "object", "required": ["analysis"]},
                preconditions=["evidence refs are present in dynamic state"],
                postconditions=["analysis result addresses the requested operation"],
                supported_media_types=[],
                side_effect=AtomicSideEffect.READ,
                risk_level="medium",
                timeout_seconds=90,
                retry_policy={"max_attempts": 1},
                planner_visible=True,
            ),
            AtomicCapabilitySpec(
                capability_key="image_understanding",
                display_name="理解图片",
                description_for_planner="Understand an attached image when the vision runtime is explicitly enabled for Dynamic Agent.",
                executor_kind="service",
                backend_target="VisionService",
                input_schema={"type": "object", "required": ["file_public_id"]},
                output_schema={"type": "object", "required": ["image_facts"]},
                preconditions=["vision runtime is stable", "file is an image"],
                postconditions=["image facts are available as evidence"],
                supported_media_types=[".png", ".jpg", ".jpeg", ".webp"],
                side_effect=AtomicSideEffect.READ,
                risk_level="medium",
                timeout_seconds=90,
                retry_policy={"max_attempts": 1},
                planner_visible=vision_stable,
            ),
        ]
        return cls(specs)

    def get(self, capability_key: str) -> AtomicCapabilitySpec | None:
        return self._by_key.get(capability_key)

    def require(self, capability_key: str) -> AtomicCapabilitySpec:
        spec = self.get(capability_key)
        if spec is None:
            raise ValueError(f"unknown_atomic_capability:{capability_key}")
        return spec

    def planner_visible(self) -> list[AtomicCapabilitySpec]:
        return [spec for spec in self._by_key.values() if spec.planner_visible]

    def validate(self) -> None:
        for spec in self._by_key.values():
            if not spec.capability_key or not spec.backend_target:
                raise ValueError("invalid_atomic_capability:missing_target")
            if spec.planner_visible and not spec.input_schema:
                raise ValueError(f"invalid_atomic_capability:missing_input_schema:{spec.capability_key}")
            if spec.planner_visible and not spec.output_schema:
                raise ValueError(f"invalid_atomic_capability:missing_output_schema:{spec.capability_key}")
            if spec.planner_visible and not spec.risk_level:
                raise ValueError(f"invalid_atomic_capability:missing_risk:{spec.capability_key}")
            if spec.planner_visible and not spec.side_effect:
                raise ValueError(f"invalid_atomic_capability:missing_side_effect:{spec.capability_key}")


# 模块定位:原子能力注册表(Dynamic Agent planner 用)
#
# 与 capability_registry 区分:
#   * capability_registry — 语义级路由 (RequestUnderstanding → Provider)
#   * atomic_capability_registry — 原语级 (Dynamic Agent planner 看)
#
# "原子能力" = 单一不可分动作(read_file / write_section / search_kb);
# planner 把 task 拆成 atomic capability 序列。
#
# 链路:
#   agent/dynamic_agent.planner
#     → atomic_capability_registry.list_by(capability_set)
#     → atomic_capability_registry.get(name) → Callable
#
# 关键约束:
#   - 注册必须 idempotent(同名覆盖不会破坏调用方);
#   - 与 planner 强绑定,**不要**改 schema 不通知 planner 团队;
#   - 与 capability_router 不互通(两个独立注册表)。
