"""ContextProfile Registry — 代码注册的 ContextProfile 目录（非数据库表）。

设计文档 §24.2：不创建数据库版 context_profiles。Profile 使用代码版本管理，
通过 ``registry.get(profile_key)`` 查找。ContextProfile 只声明输入合同；
LLMTaskProfile 通过 ``context_profile_key`` 关联。

call-site 粒度遵循设计文档 §8.1.2。一个 call-site 对应一个独立 profile key
（允许多个 call-site 复用同一 Base Policy），应用启动时验证映射完整，
缺失映射 fail-fast。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineStage,
    raise_engine_error,
)
from app.context_engine.models.context import ContextKind
from app.context_engine.models.enums import PayloadMode
from app.context_engine.models.profile import BudgetPolicy, ContextProfile, ProfileSectionSpec

# ═══════════════════════════════════════════════════════════════════════
# 1. 可复用 Base Policy（多个 call-site 共享同一输入合同）
# ═══════════════════════════════════════════════════════════════════════

# -- Base Policy: 轻量对话（chat.reply / intent.recognize）----------------
BASE_POLICY_LIGHT_DIALOG = BudgetPolicy(
    target_input_ratio=0.5,
    # Keep ordinary conversation history verbatim until 60% of the model
    # window.  This is deliberately independent from the later generic
    # hard/absolute preflight guards.
    conversation_compact_ratio=0.6,
    # Ratios apply to the usable window (200K -> 180K after reserves).
    # These values align Soft with the 65% UI warning band and Hard with
    # the 80% automatic-compression band for the primary 200K chat model.
    soft_ratio=0.72,
    hard_compact_ratio=0.89,
    absolute_ratio=0.98,
    output_reserve_tokens=4000,
    provider_overhead_tokens=1000,
)
# -- Base Policy: 标准生成（test_plan.* 主要路径）-------------------------
BASE_POLICY_STANDARD_GENERATION = BudgetPolicy(
    target_input_ratio=0.6,
    output_reserve_tokens=16000,
    provider_overhead_tokens=3000,
)
# -- Base Policy: 增量修改（incremental.*）-------------------------------
BASE_POLICY_INCREMENTAL = BudgetPolicy(
    target_input_ratio=0.6,
    output_reserve_tokens=12000,
    provider_overhead_tokens=3000,
)
# -- Base Policy: 压缩（compression.*）-----------------------------------
BASE_POLICY_COMPRESSION = BudgetPolicy(
    target_input_ratio=0.5,
    output_reserve_tokens=8000,
    provider_overhead_tokens=2000,
)
# -- Base Policy: 记忆提取（memory.extract.*）----------------------------
BASE_POLICY_MEMORY_EXTRACT = BudgetPolicy(
    target_input_ratio=0.4,
    output_reserve_tokens=4000,
    provider_overhead_tokens=1000,
)
# -- Base Policy: 测试方案准备/章节建议（轻量决策）------------------------
BASE_POLICY_DECISION_LIGHT = BudgetPolicy(
    target_input_ratio=0.5,
    output_reserve_tokens=6000,
    provider_overhead_tokens=1000,
)
# Bounded, non-conversational model calls (title, semantic classification,
# vision and task narratives).  They do not need a chat-size output reserve
# or a conversation-compaction policy, but still receive normal CE auditing.
BASE_POLICY_MICRO_TASK = BudgetPolicy(
    target_input_ratio=0.5,
    output_reserve_tokens=2000,
    provider_overhead_tokens=1000,
)

# ═══════════════════════════════════════════════════════════════════════
# 2. Profile 定义（每个 call-site 独立 key + version）
# ═══════════════════════════════════════════════════════════════════════

# -- chat.reply ----------------------------------------------------------
CHAT_REPLY_PROFILE = ContextProfile(
    key="chat.reply.v1",
    version="v1",
    description="普通对话回复：摘要 + 最近 20 轮，按模型窗口动态预算",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        # The current user message is required and must reach preflight intact.
        # A per-section cap would drop a long normal-chat message before the
        # Soft/Hard/Absolute policy can decide how to handle total context.
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=0, source_types=["conversation"]),
    ],
    optional_sections=[
        # max_budget_tokens=0 表示不限,让 selector 按总 input_budget 自动管
        # 防止长对话被硬截断;selector 在总 budget 超容时按 priority 丢
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=0, source_types=["conversation", "conversation_summary"]),
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=2000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=2000, allow_retrieval=True),
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=1000, source_types=["user_memory"]),
        # EVIDENCE 段：让 chat 能看到本会话上传的文件 + 之前任务产物
        # - file_summary: 已被 RequirementParserTool 解析的文档摘要
        # - parsed_document: 文档原文
        # - artifact / generated_content: 之前任务生成的产物
        ProfileSectionSpec(
            kind=ContextKind.EVIDENCE,
            max_budget_tokens=10000,
            source_types=["file_summary", "parsed_document", "artifact", "generated_content"],
        ),
        # TASK_STATE 段：让 chat 知道当前会话是否有进行中的任务
        ProfileSectionSpec(
            kind=ContextKind.TASK_STATE,
            max_budget_tokens=1000,
            source_types=["task_state"],
        ),
    ],
    budget_policy=BASE_POLICY_LIGHT_DIALOG,
    retrieval_policy="chat_evidence",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=True,
    fallback_chain=[],
)

# -- document.qa ----------------------------------------------------------
# A dedicated call site keeps ordinary chat from performing document search on
# every turn and makes the document evidence boundary explicit in snapshots.
DOCUMENT_QA_PROFILE = ContextProfile(
    key="document.qa.v1",
    version="v2",
    description="会话资料问答：仅使用当前会话内已索引的上传资料与生成产物作为证据。",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=0, source_types=["conversation"]),
        ProfileSectionSpec(
            kind=ContextKind.EVIDENCE,
            required=True,
            # This source supplies one atomic, locked whole-document item.
            # A per-section quota would silently turn it back into fragments.
            max_budget_tokens=0,
            source_types=["parsed_document", "artifact"],
            allow_retrieval=False,
        ),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=4000, source_types=["conversation", "conversation_summary"]),
    ],
    budget_policy=BASE_POLICY_LIGHT_DIALOG,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

# -- intent.recognize ----------------------------------------------------
INTENT_RECOGNIZE_PROFILE = ContextProfile(
    key="intent.recognize.v1",
    version="v1",
    description="意图识别调用场景：轻量上下文，保留当前消息与任务状态",
    required_sections=[
        # 意图识别必须有路由指令（intent/route 枚举 + 路由规则），否则 LLM
        # 不知道要输出意图 JSON，会按普通聊天回答 → JsonStrictParser 拒绝。
        # 生产注入见 SystemRulesSourceAdapter（intent.recognize 专用提示词）。
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        # The router's current goal is required.  Its compact representation
        # can still exceed 1,500 tokens after recent-turn context is included;
        # do not discard it before the overall preflight policy can decide.
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=0, source_types=["system", "conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=False, max_budget_tokens=800, source_types=["task_state"]),
    ],
    optional_sections=[
        # IntentRouter already puts a bounded summary + six short recent-turn
        # excerpts into CURRENT_GOAL.  Keep only a small independent safety
        # window here: routing must never inherit/compact the full chat prompt.
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=4000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=1000, allow_retrieval=True),
    ],
    budget_policy=BASE_POLICY_LIGHT_DIALOG,
    retrieval_policy="intent_scope",
    compression_policy="preserve_goal_and_task_state",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=["chat.reply.v1"],
)

# -- Small, specialized model calls ---------------------------------------
# Each of these calls has a distinct source boundary and output contract.  Do
# not map them to chat.reply.v1: that profile can retrieve unrelated history,
# evidence and memory, which is harmful for classifiers and strict parsers.
CONVERSATION_TITLE_PROFILE = ContextProfile(
    key="conversation.title.v1",
    version="v1",
    description="会话标题：只根据当前首条用户目标生成简短标题",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=800, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=800, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

FILE_UNDERSTANDING_CONTEXT_PROFILE = ContextProfile(
    key="file.understanding.v1",
    version="v1",
    description="文件语义识别：只处理受限文件样本并返回严格 JSON",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=6500, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

VISION_IMAGE_ANALYSIS_PROFILE = ContextProfile(
    key="vision.image_analysis.v1",
    version="v1",
    description="视觉内容识别：图像、OCR 提示和严格 JSON 证据输出",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=2500, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

CHAT_IMAGE_REPLY_PROFILE = ContextProfile(
    key="chat.image_reply.v1",
    version="v1",
    description="带图对话回复：当前问题、受控最近对话和项目指令",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=4000, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=4000, source_types=["conversation", "conversation_summary"]),
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=1500, source_types=["project_instruction"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

VISION_CONNECTION_PROBE_PROFILE = ContextProfile(
    key="vision.connection_probe.v1",
    version="v1",
    description="视觉连接探测：最小探测提示与图片，不读取会话上下文",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1000, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=800, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

COMPLETION_SUMMARY_PROFILE = ContextProfile(
    key="completion.summary.v1",
    version="v1",
    description="任务完成摘要：结构化任务事实与受控任务状态",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1200, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=4000, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, max_budget_tokens=3000, source_types=["task_state"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal_and_task_state",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

TEST_PLAN_TOOL_NARRATIVE_PROFILE = ContextProfile(
    key="test_plan.tool_narrative.v1",
    version="v1",
    description="测试方案工具叙事：当前工具事实与受控任务状态",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1200, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=3500, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, max_budget_tokens=3000, source_types=["task_state"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal_and_task_state",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

TEST_PLAN_TASK_SUMMARY_NARRATIVE_PROFILE = ContextProfile(
    key="test_plan.task_summary_narrative.v1",
    version="v1",
    description="测试方案任务总结叙事：最终事实、产物和任务状态",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1200, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=4500, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, max_budget_tokens=5000, source_types=["task_state"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal_and_task_state",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

DYNAMIC_AGENT_PLANNER_PROFILE = ContextProfile(
    key="dynamic_agent.planner.v1",
    version="v1",
    description="动态 Agent 规划：当前目标与任务状态，返回严格计划 JSON",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=4000, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, max_budget_tokens=5000, source_types=["task_state"]),
    ],
    budget_policy=BASE_POLICY_DECISION_LIGHT,
    retrieval_policy="none",
    compression_policy="preserve_goal_and_task_state",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

DYNAMIC_AGENT_TOOL_NARRATIVE_PROFILE = ContextProfile(
    key="dynamic_agent.tool_narrative.v1",
    version="v1",
    description="动态 Agent 工具叙事：当前工具事实与任务状态",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1200, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=3500, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, max_budget_tokens=3000, source_types=["task_state"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal_and_task_state",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

INCREMENTAL_TASK_SUMMARY_NARRATIVE_PROFILE = ContextProfile(
    key="incremental.task_summary_narrative.v1",
    version="v1",
    description="增量任务总结叙事：变更事实、受影响产物和任务状态",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1200, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=4500, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, max_budget_tokens=5000, source_types=["task_state"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal_and_task_state",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

WORD_EXPORT_PROJECT_TITLE_PROFILE = ContextProfile(
    key="word_export.project_title.v1",
    version="v1",
    description="Word 导出项目名推断：仅处理传入的受控标题线索",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=800, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1200, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_MICRO_TASK,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

# -- test_plan.preparation.decide -----------------------------------------
TEST_PLAN_PREPARATION_DECIDE_PROFILE = ContextProfile(
    key="test_plan.preparation.decide.v1",
    version="v1",
    description="测试方案 Preparation 决策：需求证据 + 模板结构 + 章节建议",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CALL_CONTRACT, required=True, max_budget_tokens=800, source_types=["system"]),
        # Preparation passes its already bounded decision payload through
        # CURRENT_GOAL.  It includes the user request plus compact parser
        # summaries, so it can legitimately exceed 1K tokens.  As with normal
        # chat, never discard the authoritative current request before the
        # model can make a decision; the total CE/preflight budget remains the
        # upper bound.
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=0, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=15000, source_types=["file_summary", "parsed_document", "template_section", "artifact"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=3000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=1200, source_types=["user_memory", "workspace_memory", "agent_playbook"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=4000, allow_retrieval=True),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=2000, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_STANDARD_GENERATION,
    retrieval_policy="preparation_evidence",
    compression_policy="preserve_evidence",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=True,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.section_suggest --------------------------------------------
TEST_PLAN_SECTION_SUGGEST_PROFILE = ContextProfile(
    key="test_plan.section_suggest.v1",
    version="v1",
    description="测试方案章节建议：需求证据 + 模板章节结构",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=1500, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=10000, source_types=["file_summary", "parsed_document"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=2000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=3000, allow_retrieval=True),
    ],
    budget_policy=BASE_POLICY_DECISION_LIGHT,
    retrieval_policy="section_suggest_evidence",
    compression_policy="preserve_evidence",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=True,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.requirement.extract ----------------------------------------
# One bounded source chunk per call.  The output is a traceable requirement
# ledger used by the final generator; it is not a replacement for the stored
# original document.
TEST_PLAN_REQUIREMENT_EXTRACT_PROFILE = ContextProfile(
    key="test_plan.requirement.extract.v1",
    version="v1",
    description="长需求分块提取：单个完整来源分块 + 覆盖元数据",
    required_sections=[
        ProfileSectionSpec(
            kind=ContextKind.SYSTEM_RULES,
            required=True,
            max_budget_tokens=1500,
            source_types=["system"],
        ),
        ProfileSectionSpec(
            kind=ContextKind.CURRENT_GOAL,
            required=True,
            max_budget_tokens=8000,
            source_types=["conversation"],
        ),
    ],
    optional_sections=[],
    budget_policy=BASE_POLICY_DECISION_LIGHT,
    retrieval_policy=None,
    compression_policy="preserve_current_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

# -- test_plan.generate.outline -------------------------------------------
TEST_PLAN_GENERATE_OUTLINE_PROFILE = ContextProfile(
    key="test_plan.generate.outline.v1",
    version="v1",
    description="测试方案生成·大纲：需求 + 模板 + 章节确认",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CALL_CONTRACT, required=True, max_budget_tokens=800, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=20000, source_types=["parsed_document", "template_section", "artifact"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=3000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=1200, source_types=["user_memory", "workspace_memory", "agent_playbook"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=4000, allow_retrieval=True),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=12000, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_STANDARD_GENERATION,
    retrieval_policy="generation_evidence",
    compression_policy="preserve_evidence_and_sections",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=True,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.generate.batch ---------------------------------------------
TEST_PLAN_GENERATE_BATCH_PROFILE = ContextProfile(
    key="test_plan.generate.batch.v1",
    version="v1",
    description="测试方案生成·批量章节：大纲 + 已生成正文 + 需求",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CALL_CONTRACT, required=True, max_budget_tokens=800, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=25000, source_types=["parsed_document", "artifact", "generated_content"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=3000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=1200, source_types=["user_memory", "workspace_memory", "agent_playbook"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=5000, allow_retrieval=True),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=2000, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_STANDARD_GENERATION,
    retrieval_policy="generation_evidence",
    compression_policy="preserve_evidence_and_generated",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=True,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.review -----------------------------------------------------
TEST_PLAN_REVIEW_PROFILE = ContextProfile(
    key="test_plan.review.v1",
    version="v1",
    description="测试方案审查：保留已生成正文与审查标准",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CALL_CONTRACT, required=True, max_budget_tokens=800, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        # JSON truncation can remove every generated section.  In that recovery
        # path the review findings are the only phase-local Evidence available,
        # so they must satisfy this required section and let regeneration run.
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=15000, source_types=["generated_content", "artifact", "review_result"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=3000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=1000, source_types=["user_memory", "workspace_memory", "agent_playbook"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=4000, allow_retrieval=True),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=2000, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_STANDARD_GENERATION,
    retrieval_policy="review_evidence",
    compression_policy="preserve_generated_sections",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=True,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.repair.plan -------------------------------------------------
TEST_PLAN_REPAIR_PLAN_PROFILE = ContextProfile(
    key="test_plan.repair.plan.v1",
    version="v1",
    description="测试方案修复·计划：审查结果 + 待修章节",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CALL_CONTRACT, required=True, max_budget_tokens=800, source_types=["system"]),
        # Repair prompts contain the bounded review contract and may legitimately
        # exceed the generic one-thousand-token goal allowance. CURRENT_GOAL is
        # required, so an undersized budget drops the whole prompt before the
        # recovery path can reach the model.
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=2500, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=12000, source_types=["generated_content", "artifact", "review_result"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=3000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=1000, source_types=["user_memory", "workspace_memory", "agent_playbook"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=3000, allow_retrieval=True),
    ],
    budget_policy=BASE_POLICY_STANDARD_GENERATION,
    retrieval_policy="repair_evidence",
    compression_policy="preserve_generated_sections",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=True,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.repair.regenerate ------------------------------------------
TEST_PLAN_REPAIR_REGENERATE_PROFILE = ContextProfile(
    key="test_plan.repair.regenerate.v1",
    version="v1",
    description="测试方案修复·重新生成：修复指令 + 原章节",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CALL_CONTRACT, required=True, max_budget_tokens=800, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=2500, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        # A truncated generation has no generated section body yet. Its review
        # findings must therefore satisfy required Evidence so recovery can run.
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=15000, source_types=["generated_content", "artifact", "review_result"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=3000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=1000, source_types=["user_memory", "workspace_memory", "agent_playbook"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=3000, allow_retrieval=True),
    ],
    budget_policy=BASE_POLICY_STANDARD_GENERATION,
    retrieval_policy="repair_evidence",
    compression_policy="preserve_generated_sections",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=True,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.repair.review ----------------------------------------------
TEST_PLAN_REPAIR_REVIEW_PROFILE = ContextProfile(
    key="test_plan.repair.review.v1",
    version="v1",
    description="测试方案修复·复审：修复后章节 + 审查标准",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CALL_CONTRACT, required=True, max_budget_tokens=800, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=2500, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=15000, source_types=["generated_content", "artifact"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=3000, source_types=["project_instruction"]),
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=1000, source_types=["user_memory", "workspace_memory", "agent_playbook"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=3000, allow_retrieval=True),
    ],
    budget_policy=BASE_POLICY_STANDARD_GENERATION,
    retrieval_policy="review_evidence",
    compression_policy="preserve_generated_sections",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=True,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.incremental.diff -------------------------------------------
INCREMENTAL_DIFF_PROFILE = ContextProfile(
    key="test_plan.incremental.diff.v1",
    version="v1",
    description="增量修改：保留旧 Artifact 与修改指令",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1500, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=20000, source_types=["artifact", "generated_content"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=2000, allow_retrieval=True),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=1500, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_INCREMENTAL,
    retrieval_policy="incremental_evidence",
    compression_policy="preserve_artifact_and_instruction",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=["chat.reply.v1"],
)

# -- test_plan.incremental.regenerate -------------------------------------
INCREMENTAL_REGENERATE_PROFILE = ContextProfile(
    key="test_plan.incremental.regenerate.v1",
    version="v1",
    description="增量修改·重新生成：diff 结果 + 原正文",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1500, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=20000, source_types=["artifact", "generated_content"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=2000, allow_retrieval=True),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=1500, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_INCREMENTAL,
    retrieval_policy="incremental_evidence",
    compression_policy="preserve_artifact_and_instruction",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=["chat.reply.v1"],
)

# -- memory.extract.* -----------------------------------------------------
MEMORY_EXTRACT_USER_PROFILE = ContextProfile(
    key="memory.extract.user.v1",
    version="v1",
    description="用户记忆提取：当前对话 + 摘要",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, required=True, max_budget_tokens=4000, source_types=["conversation", "conversation_summary"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=2000, source_types=["user_memory"]),
    ],
    budget_policy=BASE_POLICY_MEMORY_EXTRACT,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

MEMORY_EXTRACT_WORKSPACE_PROFILE = ContextProfile(
    key="memory.extract.workspace.v1",
    version="v1",
    description="工作区记忆提取：对话 + 工作区上下文",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, required=True, max_budget_tokens=4000, source_types=["conversation", "conversation_summary"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=2000, source_types=["workspace_memory"]),
    ],
    budget_policy=BASE_POLICY_MEMORY_EXTRACT,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

MEMORY_EXTRACT_PLAYBOOK_PROFILE = ContextProfile(
    key="memory.extract.playbook.v1",
    version="v1",
    description="Agent Playbook 记忆提取：对话 + Agent 经验",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, required=True, max_budget_tokens=4000, source_types=["conversation", "conversation_summary"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.MEMORY, max_budget_tokens=2000, source_types=["agent_playbook"]),
    ],
    budget_policy=BASE_POLICY_MEMORY_EXTRACT,
    retrieval_policy="none",
    compression_policy="preserve_goal",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

# -- compression.* ---------------------------------------------------------
COMPRESSION_CONVERSATION_PROFILE = ContextProfile(
    key="compression.conversation.v1",
    version="v1",
    description="对话压缩：摘要 + 最近轮次 + 保留锚点",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, required=True, max_budget_tokens=8000, source_types=["conversation", "conversation_summary"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=2000, source_types=["project_instruction"]),
    ],
    budget_policy=BASE_POLICY_COMPRESSION,
    retrieval_policy="none",
    compression_policy="conversation_compaction",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

# Manual compaction has the same source needs as automatic conversation
# compaction, but it remains an independent production call site.  Keeping a
# distinct key preserves per-call-site auditability and avoids policy changes
# for one path silently affecting the other.
COMPRESSION_CONVERSATION_MANUAL_PROFILE = ContextProfile(
    key="compression.conversation.manual.v1",
    version="v1",
    description="手动对话压缩：受保护锚点 + 历史对话，生成可恢复摘要",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, required=True, max_budget_tokens=8000, source_types=["conversation", "conversation_summary"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.PROJECT_INSTRUCTIONS, max_budget_tokens=2000, source_types=["project_instruction"]),
    ],
    budget_policy=BASE_POLICY_COMPRESSION,
    retrieval_policy="none",
    compression_policy="conversation_compaction",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)


COMPRESSION_AGENT_LOOP_PROFILE = ContextProfile(
    key="compression.agent_loop.v1",
    version="v1",
    description="Agent 循环压缩：任务状态 + 最近步骤",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1000, source_types=["conversation"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=4000, source_types=["task_state"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=3000, source_types=["conversation", "conversation_summary"]),
    ],
    budget_policy=BASE_POLICY_COMPRESSION,
    retrieval_policy="none",
    compression_policy="agent_loop_compaction",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

COMPRESSION_EVIDENCE_PROFILE = ContextProfile(
    key="compression.evidence.v1",
    version="v1",
    description="证据段压缩：需求/模板/正文压缩",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, required=True, max_budget_tokens=15000, source_types=["parsed_document", "artifact", "generated_content"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=2000, source_types=["conversation"]),
    ],
    budget_policy=BASE_POLICY_COMPRESSION,
    retrieval_policy="none",
    compression_policy="evidence_compaction",
    tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

# -- ce.pilot.summarize --------------------------------------------------
CE_PILOT_SUMMARIZE_PROFILE = ContextProfile(
    key="ce.pilot.summarize.v1",
    version="v1",
    description="Context Engine Pilot 摘要：系统规则 + 任务状态 + 证据 + 当前消息",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, required=True, max_budget_tokens=2000, source_types=["task_state"]),
    ],
    optional_sections=[
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, max_budget_tokens=8000, source_types=["parsed_document", "artifact", "file_summary"]),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=2000, source_types=["conversation", "conversation_summary"]),
        ProfileSectionSpec(kind=ContextKind.KNOWLEDGE, max_budget_tokens=1000, allow_retrieval=True),
    ],
    budget_policy=BASE_POLICY_DECISION_LIGHT,
    retrieval_policy="pilot_evidence",
    compression_policy="preserve_goal_and_task_state",
    tool_output_policy=PayloadMode.REFERENCE,
    allow_long_term_memory=False,
    fallback_chain=[],
)

# ═══════════════════════════════════════════════════════════════════════
# 3. call-site → profile key 映射表
# ═══════════════════════════════════════════════════════════════════════
# 来源：CE-00 LLM Call Inventory（03_llm_call_inventory.md §2）
# 粒度：设计文档 §8.1.2 call-site 粒度。

COMPRESSION_FULL_REPLACE_PROFILE = ContextProfile(
    key="compression.full_replace.v1", version="v1",
    description="Full context replacement: protected anchors and compact recovery evidence.",
    required_sections=[
        ProfileSectionSpec(kind=ContextKind.SYSTEM_RULES, required=True, max_budget_tokens=1500, source_types=["system"]),
        ProfileSectionSpec(kind=ContextKind.CURRENT_GOAL, required=True, max_budget_tokens=1500, source_types=["conversation"]),
    ], optional_sections=[
        ProfileSectionSpec(kind=ContextKind.TASK_STATE, max_budget_tokens=4000, source_types=["task_state"]),
        ProfileSectionSpec(kind=ContextKind.CONVERSATION, max_budget_tokens=8000, source_types=["conversation", "conversation_summary"]),
        ProfileSectionSpec(kind=ContextKind.EVIDENCE, max_budget_tokens=12000, source_types=["parsed_document", "artifact", "generated_content"]),
    ], budget_policy=BASE_POLICY_COMPRESSION, retrieval_policy="none",
    compression_policy="full_replace", tool_output_policy=PayloadMode.EXCLUDE,
    allow_long_term_memory=False, fallback_chain=[],
)

CALL_SITE_TO_PROFILE_KEY: dict[str, str] = {
    # 通用
    "chat.reply": "chat.reply.v1",
    "document.qa": "document.qa.v1",
    "intent.recognize": "intent.recognize.v1",
    # WP-BE-09 fix: conversation.title（MIG_SUMMARY=true 时 MessageService 调用）
    # 走 ContextInvokerBridge，需要对应 profile；title 生成是轻量文本，复用
    # chat.reply 的轻量输入合同（系统规则 + 当前目标，无 task 依赖）。
    "conversation.title": "conversation.title.v1",
    # Uploaded-file semantic profiling is a bounded, user-scoped JSON task.
    # Its output contract comes from FILE_UNDERSTANDING_PROFILE; the CE
    # profile supplies snapshot, sanitation and budget governance.
    "file.understanding": "file.understanding.v1",
    "vision.image_analysis": "vision.image_analysis.v1",
    "chat.image_reply": "chat.image_reply.v1",
    "vision.connection_probe": "vision.connection_probe.v1",
    # Lightweight public-facing summaries/narratives still need the same
    # scope, snapshot and audit boundary as other Agent LLM calls.
    "completion.summary": "completion.summary.v1",
    "test_plan.tool_narrative": "test_plan.tool_narrative.v1",
    "test_plan.task_summary_narrative": "test_plan.task_summary_narrative.v1",
    # 测试方案 Preparation / 章节建议
    "test_plan.preparation.decide": "test_plan.preparation.decide.v1",
    "test_plan.section_suggest": "test_plan.section_suggest.v1",
    "test_plan.requirement.extract": "test_plan.requirement.extract.v1",
    # 测试方案生成
    "test_plan.generate.outline": "test_plan.generate.outline.v1",
    "test_plan.generate.batch": "test_plan.generate.batch.v1",
    # 测试方案 Review
    "test_plan.review": "test_plan.review.v1",
    # 测试方案 Repair
    "test_plan.repair.plan": "test_plan.repair.plan.v1",
    "test_plan.repair.regenerate": "test_plan.repair.regenerate.v1",
    "test_plan.repair.review": "test_plan.repair.review.v1",
    # 测试方案 Incremental
    "test_plan.incremental.diff": "test_plan.incremental.diff.v1",
    "test_plan.incremental.regenerate": "test_plan.incremental.regenerate.v1",
    # 记忆提取
    "memory.extract.user": "memory.extract.user.v1",
    "memory.extract.workspace": "memory.extract.workspace.v1",
    "memory.extract.playbook": "memory.extract.playbook.v1",
    # 压缩
    "compression.conversation": "compression.conversation.v1",
    # 手动压缩与自动压缩采用相近的输入合同，但保留独立 Profile，
    # 以便审计和后续策略演进不会相互影响。
    "compression.conversation.manual": "compression.conversation.manual.v1",
    "compression.agent_loop": "compression.agent_loop.v1",
    "compression.evidence": "compression.evidence.v1",
    "compression.full_replace": "compression.full_replace.v1",
    # CE-02 Pilot
    "ce.pilot.summarize": "ce.pilot.summarize.v1",
    # Dynamic Agent planner uses ContextInvokerBridge for optional LLM
    # planning. It needs system rules, current goal, and task state; the
    # CE pilot profile already provides that compact input contract.
    "dynamic_agent.planner": "dynamic_agent.planner.v1",
    "dynamic_agent.tool_narrative": "dynamic_agent.tool_narrative.v1",
    "incremental.task_summary_narrative": "incremental.task_summary_narrative.v1",
    # BUG FIX 2026-08-18: WordExportTool 在 MIG_GENERATE=true 时经
    # ContextInvokerBridge 调用 LLM 推断项目名
    # (word_export_tool.py:395 call_site="word_export.project_title")。
    # task 是轻量纯文本生成(2-6 字项目名、max_tokens=64、无 JSON 合同),
    # 与现有 conversation.title → chat.reply.v1 同类场景,复用 chat.reply.v1
    # 的轻量输入合同(系统规则 + 当前目标)完全匹配,不增加 profile 数量
    # (保持 19 个内置 profile 不变量)。
    # missing mapping → ContextEngineFailure(code=context.profile.no_call_site_mapping)
    # → MIGRATION_INVOKER_EXCEPTION,MIG=true 路径静默失败。
    "word_export.project_title": "word_export.project_title.v1",
}

# 已知 LLMTaskProfile 的显式映射（F014 输出合同 → ContextProfile 输入合同）
LLMTASK_PROFILE_TO_CONTEXT_PROFILE: dict[str, str] = {
    "CHAT": "chat.reply.v1",
    "INTENT": "intent.recognize.v1",
    "TITLE": "conversation.title.v1",
    "TEST_PLAN": "test_plan.generate.batch.v1",
    "REQUIREMENT_EVIDENCE_EXTRACT": "test_plan.requirement.extract.v1",
    "SUMMARY": "compression.conversation.v1",
    "PREPARATION": "test_plan.preparation.decide.v1",
    "dynamic_agent_planner": "dynamic_agent.planner.v1",
    "REPAIR": "test_plan.repair.plan.v1",
    "INCREMENTAL": "test_plan.incremental.diff.v1",
    "TOOL_NARRATIVE_COMPOSER": "test_plan.tool_narrative.v1",
    "TASK_SUMMARY_NARRATIVE_COMPOSER": "test_plan.task_summary_narrative.v1",
    "NARRATIVE_SCHEMA_REPAIR": "test_plan.tool_narrative.v1",
}

_BUILTIN_PROFILES: list[ContextProfile] = [
    CHAT_REPLY_PROFILE,
    DOCUMENT_QA_PROFILE,
    INTENT_RECOGNIZE_PROFILE,
    CONVERSATION_TITLE_PROFILE,
    FILE_UNDERSTANDING_CONTEXT_PROFILE,
    VISION_IMAGE_ANALYSIS_PROFILE,
    CHAT_IMAGE_REPLY_PROFILE,
    VISION_CONNECTION_PROBE_PROFILE,
    COMPLETION_SUMMARY_PROFILE,
    TEST_PLAN_TOOL_NARRATIVE_PROFILE,
    TEST_PLAN_TASK_SUMMARY_NARRATIVE_PROFILE,
    DYNAMIC_AGENT_PLANNER_PROFILE,
    DYNAMIC_AGENT_TOOL_NARRATIVE_PROFILE,
    INCREMENTAL_TASK_SUMMARY_NARRATIVE_PROFILE,
    WORD_EXPORT_PROJECT_TITLE_PROFILE,
    TEST_PLAN_PREPARATION_DECIDE_PROFILE,
    TEST_PLAN_SECTION_SUGGEST_PROFILE,
    TEST_PLAN_REQUIREMENT_EXTRACT_PROFILE,
    TEST_PLAN_GENERATE_OUTLINE_PROFILE,
    TEST_PLAN_GENERATE_BATCH_PROFILE,
    TEST_PLAN_REVIEW_PROFILE,
    TEST_PLAN_REPAIR_PLAN_PROFILE,
    TEST_PLAN_REPAIR_REGENERATE_PROFILE,
    TEST_PLAN_REPAIR_REVIEW_PROFILE,
    INCREMENTAL_DIFF_PROFILE,
    INCREMENTAL_REGENERATE_PROFILE,
    MEMORY_EXTRACT_USER_PROFILE,
    MEMORY_EXTRACT_WORKSPACE_PROFILE,
    MEMORY_EXTRACT_PLAYBOOK_PROFILE,
    COMPRESSION_CONVERSATION_PROFILE,
    COMPRESSION_CONVERSATION_MANUAL_PROFILE,
    COMPRESSION_AGENT_LOOP_PROFILE,
    COMPRESSION_EVIDENCE_PROFILE,
    CE_PILOT_SUMMARIZE_PROFILE,
    COMPRESSION_FULL_REPLACE_PROFILE,
]


class ContextProfileRegistry:
    """ContextProfile 代码注册表。

    启动时验证：
    - 每个 call-site 映射到的 profile key 都已注册（缺失 fail-fast）；
    - 每个映射 profile 有 version。
    """

    def __init__(
        self,
        profiles: list[ContextProfile] | None = None,
        call_site_map: dict[str, str] | None = None,
    ) -> None:
        self._profiles: dict[str, ContextProfile] = {}
        for profile in profiles or _BUILTIN_PROFILES:
            self.register(profile)
        self._call_site_map = dict(call_site_map or CALL_SITE_TO_PROFILE_KEY)
        self.validate()

    def register(self, profile: ContextProfile) -> None:
        if profile.key in self._profiles:
            raise ValueError(f"ContextProfile 重复注册: {profile.key}")
        self._profiles[profile.key] = profile

    def get(self, key: str) -> ContextProfile:
        profile = self._profiles.get(key)
        if profile is None:
            raise_engine_error(
                code="context.profile.not_found",
                detail=f"未注册的 ContextProfile: {key!r}",
                stage=ContextEngineStage.PROFILE,
                retryable=False,
                recoverable=True,
                safe_metadata={"profile_key": key},
            )
        return profile

    def get_or_none(self, key: str) -> ContextProfile | None:
        return self._profiles.get(key)

    def get_for_call_site(self, call_site: str) -> ContextProfile:
        """call-site → profile 解析（缺失 fail-fast）。"""
        key = self._call_site_map.get(call_site)
        if key is None:
            raise_engine_error(
                code="context.profile.no_call_site_mapping",
                detail=f"call_site 未映射到 ContextProfile: {call_site!r}",
                stage=ContextEngineStage.PROFILE,
                retryable=False,
                recoverable=True,
                safe_metadata={"call_site": call_site},
            )
        return self.get(key)

    def keys(self) -> list[str]:
        return list(self._profiles.keys())

    def call_site_keys(self) -> list[str]:
        return list(self._call_site_map.keys())

    def validate(self) -> None:
        """验证映射完整：所有 call-site 指向已注册 profile；缺失 fail-fast。"""
        missing: list[str] = []
        for call_site, key in self._call_site_map.items():
            profile = self._profiles.get(key)
            if profile is None:
                missing.append(f"{call_site} → {key}")
        if missing:
            raise_engine_error(
                code="context.profile.incomplete_mapping",
                detail=f"call-site → profile 映射缺失: {missing}",
                stage=ContextEngineStage.PROFILE,
                retryable=False,
                recoverable=True,
                safe_metadata={"missing_count": len(missing)},
            )


_default_registry = ContextProfileRegistry()


def get_default_profile_registry() -> ContextProfileRegistry:
    """返回默认注册表（内置 18 个 Profile + call-site 映射）。"""
    return _default_registry
# auto-appended module-level note: profile registry: 维护 CHAT / TEST_PLAN / SUMMARY 等 profile 配置。
