"""Phase 2.9B.4 — Narrative Composer 核心领域模型。

设计约束(镜像 preparation/repair/incremental schemas):
* 所有模型必须 JSON 可序列化 (Rule 10)。
* 字段长度上限防 Chain-of-Thought 膨胀。
* 复用现有 ``AgentPublicUpdateDraft``(public_narrative.py)作为用户可见叙事合同,
  不得创建第二套相互冲突的展示合同。
* NarrativeContext 是经过白名单压缩的事实快照,绝不包含完整 Graph State /
  系统路径 / API Key / 异常堆栈。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.agent_runtime._shared.public_narrative import AgentPublicUpdateDraft


# ── Narrative kind / status ───────────────────────────────────────────────


NarrativeKind = Literal["tool", "task_summary"]
NarrativeSource = Literal["llm", "deterministic"]
NarrativeStatus = Literal["streaming", "completed", "failed", "fallback"]
ToolTerminalStatus = Literal["success", "failed", "skipped", "retry_scheduled", "timeout"]


class NarrativeFactConstraints(BaseModel):
    """事实约束白名单 — 校验模型叙事是否虚构事实。

    Phase 2.9B.5 拆分数字事实与字面量事实:
    * ``allowed_numeric_facts`` — 允许出现的真实统计数字(43/7/3 等)。
    * ``allowed_literal_facts`` — 允许出现的完整字面量(文件名 / 产物名 /
      版本号等)。这些字面量内嵌的数字(如 ``file_dc6d128c`` 的 6/128、
      ``03_xxx.docx`` 的 03)在数字扫描前整串遮蔽,不得单独判定为虚构数字。
    * ``numeric_exempt_literals`` — 需要豁免但其内部数字不应作为统计数字
      参与校验的完整字面量;语义与 ``allowed_literal_facts`` 一致,保留为
      别名以便 Builder 把「用户可见字面量」与「纯豁免字面量」分开登记。
    """

    model_config = ConfigDict(extra="forbid")

    allowed_numeric_facts: List[int] = Field(default_factory=list)
    allowed_file_names: List[str] = Field(default_factory=list)
    allowed_literal_facts: List[str] = Field(default_factory=list)
    numeric_exempt_literals: List[str] = Field(default_factory=list)
    forbidden_internal_fields: List[str] = Field(
        default_factory=lambda: [
            "system_prompt",
            "api_key",
            "stack_trace",
            "server_path",
            "internal_id",
            "database",
        ]
    )


class ToolNarrativeContext(BaseModel):
    """单个 Tool 完成后的压缩事实快照。

    只允许白名单事实,不传完整 Graph State / 完整文档 / 系统路径。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    task_id: str = ""
    graph_run_id: str = ""
    goal: str = ""
    current_phase: str = ""
    tool_name: str
    tool_call_id: str = ""
    source_event_id: str = ""
    attempt: int = 1
    terminal_status: ToolTerminalStatus
    duration_ms: Optional[int] = None
    input_facts: Dict[str, Any] = Field(default_factory=dict)
    output_facts: Dict[str, Any] = Field(default_factory=dict)
    state_diff: Dict[str, Any] = Field(default_factory=dict)
    execution_context: Dict[str, Any] = Field(default_factory=dict)
    fact_constraints: NarrativeFactConstraints = Field(
        default_factory=NarrativeFactConstraints
    )


class TaskSummaryNarrativeContext(BaseModel):
    """最终任务总结的压缩事实快照。

    BUG FIX 2026-08-18 (B2): 增加 ``requirement_text_excerpt`` 与
    ``generated_section_content_excerpts`` 两个 optional 字段,供 LLM
    在写最终总结时结合真实需求文档要点与生成章节内容展开叙述,
    而非仅基于数字 / 文件名等结构化事实生成格式化模板。

    字段默认 ``None`` 时向后兼容(老调用方不需要传),LLM prompt 用
    ``model_dump(exclude_none=True)`` 序列化时不显示。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    task_id: str = ""
    graph_run_id: str = ""
    task_goal: str = ""
    task_status: str = "completed"
    completed_tools: List[Dict[str, Any]] = Field(default_factory=list)
    generated_sections: Optional[int] = None
    preserved_template_sections: Optional[int] = None
    business_modules: Optional[int] = None
    review: Dict[str, Any] = Field(default_factory=dict)
    artifact: Dict[str, Any] = Field(default_factory=dict)
    important_decisions: List[str] = Field(default_factory=list)
    repairs_performed: List[str] = Field(default_factory=list)
    remaining_risks: List[str] = Field(default_factory=list)
    # BUG FIX 2026-08-18 (B2): 摘要内容上下文
    # requirement_text_excerpt  — 需求文档正文摘要(默认前 600 字)
    # generated_section_content_excerpts — 关键章节内容摘要
    #   格式:[{"section_id": str, "title": str, "excerpt": str}]
    #   默认取前 5 个章节,每个前 200 字,token 经济。
    requirement_text_excerpt: Optional[str] = None
    generated_section_content_excerpts: Optional[List[Dict[str, Any]]] = None
    fact_constraints: NarrativeFactConstraints = Field(
        default_factory=NarrativeFactConstraints
    )


# ── Generation request / stream chunk / result ────────────────────────────


class NarrativeGenerationRequest(BaseModel):
    """一次叙事生成的完整输入。"""

    model_config = ConfigDict(extra="forbid")

    kind: NarrativeKind
    narrative_id: str
    generation_id: str
    generation_no: int
    tool_context: Optional[ToolNarrativeContext] = None
    task_summary_context: Optional[TaskSummaryNarrativeContext] = None
    system_prompt: str
    user_content: str
    schema_version: int = 1


class NarrativeStreamChunk(BaseModel):
    """解码后的 field 级增量。"""

    model_config = ConfigDict(extra="forbid")

    field: Literal["headline", "summary", "impact", "next_action", "details", "narrative_text"]
    delta: str
    chunk_index: int


class NarrativeValidationResult(BaseModel):
    """Schema + 事实校验结果。"""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: List[str] = Field(default_factory=list)
    fact_feedback: Optional[str] = None


class NarrativeGenerationResult(BaseModel):
    """一次叙事生成的终态结果。"""

    model_config = ConfigDict(extra="forbid")

    narrative_id: str
    generation_id: str
    generation_no: int
    success: bool
    source: NarrativeSource = "deterministic"
    status: NarrativeStatus = "fallback"
    public_update: Optional[AgentPublicUpdateDraft] = None
    failure_category: Optional[str] = None
    fallback_used: bool = False
    delta_count: int = 0


# ── State fragment persisted on Graph State ───────────────────────────────


class PendingNarrative(BaseModel):
    """Tool 节点写入、Narrative Barrier 消费的待叙事片段。"""

    model_config = ConfigDict(extra="forbid")

    narrative_kind: Literal["tool"] = "tool"
    source_tool_name: str
    source_tool_call_id: str
    source_event_id: str = ""
    tool_attempt: int = 1
    terminal_status: ToolTerminalStatus
    continuation_route: str
    duration_ms: Optional[int] = None


__all__ = [
    "NarrativeFactConstraints",
    "NarrativeGenerationRequest",
    "NarrativeGenerationResult",
    "NarrativeKind",
    "NarrativeSource",
    "NarrativeStatus",
    "NarrativeStreamChunk",
    "NarrativeValidationResult",
    "PendingNarrative",
    "TaskSummaryNarrativeContext",
    "ToolNarrativeContext",
    "ToolTerminalStatus",
]
