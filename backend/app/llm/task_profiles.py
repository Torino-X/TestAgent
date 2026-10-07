"""LLM task contract profiles (F014).

Each ``LLMTaskProfile`` describes the **output contract** of one named
LLM call site: system prompt, parser type, allowed Markdown, JSON
strictness, optional output schema, and what to do when parsing fails.

Profiles are declarative — they do not hold an ``LLMClient`` instance,
do not read environment variables, and do not perform any network I/O.
They are passed into ``LLMClient.generate_with_profile`` as data.

Adding a new contract is a one-liner: instantiate another
``LLMTaskProfile`` and register it in this module.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Enums ───────────────────────────────────────────────────────────


class LLMParserType(str, Enum):
    """Which parser adapter to apply to the model's raw text."""

    JSON_STRICT = "json_strict"
    JSON_RELAXED = "json_relaxed"
    PLAIN_TEXT = "plain_text"
    MARKDOWN = "markdown"
    RAW_TEXT = "raw_text"


class LLMParseFailurePolicy(str, Enum):
    """What to do when the parser cannot extract a valid result."""

    FALLBACK_DEFAULT = "fallback_default"
    RAISE = "raise"
    # RETRY is reserved for a future batch — the wrapper currently
    # treats it the same as RAISE (raises LLMProfileParseError).
    RETRY = "retry"


# ── Profile ─────────────────────────────────────────────────────────


class LLMTaskProfile(BaseModel):
    """Declarative contract for a single LLM call site.

    Notes
    -----
    * ``system_prompt`` is the only piece of prompt owned by the
      profile.  For test-plan generation the prompt is built
      dynamically by ``PromptBuilder``; the profile only registers the
      contract, not the prompt.
    * ``parser`` selects a registered ``ParserAdapter``.
      in ``app.llm.parsers.registry``.
    * ``on_parse_failure`` is consulted **only** when the underlying
      LLM call succeeded but parsing failed.  LLM transport errors
      are always surfaced as ``LLMClientError`` and are not affected
      by this policy — the caller decides what to do with those.
    """

    name: str
    system_prompt: str
    parser: LLMParserType = LLMParserType.PLAIN_TEXT

    # Output contract flags
    allow_markdown: bool = False
    require_json: bool = False

    # Model call knobs (all optional — fall through to LLMClient defaults)
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    timeout_override: Optional[int] = None

    # Optional JSON schema for downstream structured-output validation.
    # Reserved for future contracts (RequirementExtractor, etc.).
    output_schema: Optional[dict[str, Any]] = Field(default=None)

    # Failure handling
    on_parse_failure: LLMParseFailurePolicy = LLMParseFailurePolicy.RAISE
    fallback_text: Optional[str] = None


# ── Shared fallback text constants (F014 closeout) ────────────────
#
# These are user-facing strings surfaced when the LLM call OR parse
# fails for a chat-flavoured contract.  They live in ``task_profiles``
# because both ``ChatLLMService`` and ``MessageService`` need to read
# them, and the two modules cannot import each other (top-level cycle).
# Putting the canonical string here breaks the cycle: both consumers
# import this module; this module imports nothing from them.

CHAT_FALLBACK_REPLY: str = "抱歉，临时无法回复，请稍后再试。"


# ── Built-in profiles ───────────────────────────────────────────────
#
# These four profiles are the F014 contract surface.  ChatLLMService,
# IntentRouter, and MessageService's title generator consume them.
# TestPlanGeneratorTool is NOT migrated in this batch — it keeps its
# existing PromptBuilder + ResultParser pipeline, which is stable.


# ── 1. CHAT_PROFILE — ordinary Q&A reply ──────────────────────────


CHAT_SYSTEM_PROMPT = (
    "你是 TestAgent，一个面向软件测试工作的对话式 AI 助手。"
    "你可以回答软件测试、测试方案、测试策略、测试流程、"
    "TestAgent 使用方法等问题。"
    "允许使用 Markdown，让回答结构清晰。"
    "如果用户要求生成测试方案，请提醒用户上传需求文档和测试方案模板，"
    "或说明系统会进入生成流程。"
    "如果用户要求生成测试用例、PPT、Excel 或知识库问答，"
    "说明当前版本暂未开放。"
    "不要声称你已经读取了未解析的文件内容。"
    "回答应专业、清晰、简洁。"
    "\n\n"
    "【上下文使用规则】"
    "当用户消息包含【会话摘要】【最近对话】等上下文信息时，"
    "你必须结合上下文回答问题。"
    "如果上下文中有相关信息（如对话历史、文件内容），"
    "请直接基于上下文回答，不要说「我不知道」或「不在我的专业范围内」。"
    "上下文中的信息优先级高于你的默认知识。"
    "\n\n"
    "【知识库参考使用规则】"
    "当用户消息包含【知识库参考】段时："
    "1. 优先基于其中的内容回答，并在引用处用「（来源：{文档名}）」标注；"
    "2. 如果【知识库参考】中的内容与当前问题无关或置信度偏低，"
    "明确说明「知识库中暂无直接相关内容，以下基于通用知识回答」；"
    "3. 不得编造参考片段中不存在的数字、人名、日期、版本号。"
    "\n\n"
    "【项目资料事实约束】\n"
    "当问题要求依据当前项目、需求文档或知识参考回答事实时，仅能使用提供的项目资料或知识参考中的明确事实。"
    "资料未定义或当前参考未覆盖的字段，必须明确说明“资料未定义”或“当前资料未提供”，不得以通用知识、惯例或猜测补全。"
    "不得把资料中的概括性标题扩展为未出现的具体规则；每项项目事实应标注其资料来源。"
)

CHAT_PROFILE = LLMTaskProfile(
    name="chat_reply",
    system_prompt=CHAT_SYSTEM_PROMPT,
    parser=LLMParserType.MARKDOWN,
    allow_markdown=True,
    require_json=False,
    max_tokens=2000,
    temperature=0.7,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=CHAT_FALLBACK_REPLY,
)


# ── 2. INTENT_PROFILE — user-intent classification ────────────────
#
# Note: the full system prompt (with enum constraints and routing
# rules) is held by ``app.agent.intent_router._INTENT_SYSTEM_PROMPT``.
# Keeping it there lets IntentRouter own its classifier semantics,
# while this profile registers the **contract** (strict JSON, no
# Markdown, fallback to clarify JSON).

INTENT_FALLBACK_JSON = (
    '{"intent":"unknown","route":"clarify","supported":false,'
    '"confidence":0.0,"reason":"intent_parse_failed",'
    '"need_files":false,"required_file_types":[],'
    '"missing_file_types":[],'
    '"reply":null,'
    '"reply_message":"我还不能确定你要执行哪类任务。'
    '你是想生成测试方案、生成测试用例，还是咨询某个问题？"}'
)

INTENT_PROFILE = LLMTaskProfile(
    name="intent_recognition",
    system_prompt="",  # filled in by IntentRouter at call time
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=1500,
    temperature=0.0,
    # Intent classification gates every interactive message.  Keep it bounded
    # rather than inheriting a user's multi-minute generation timeout, but
    # allow one normal provider jitter window: the real chat scenario saw a
    # valid classification call complete just after 20s.
    timeout_override=30,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=INTENT_FALLBACK_JSON,
)


# ── 3. TITLE_PROFILE — auto conversation title ────────────────────


TITLE_SYSTEM_PROMPT = (
    "只输出标题，禁止输出任何其他内容。\n"
    "输入是用户的第一句话，输出一个12字以内的中文标题。\n"
    "禁止输出解释、理由、引号、句号、冒号、URL。\n"
    "示例输入：今天天气怎么样？\n"
    "示例输出：天气咨询\n"
    "示例输入：如何系统性学习接口测试？\n"
    "示例输出：系统性学习接口测试\n"
    "示例输入：小明喜欢小红\n"
    "示例输出：闲聊"
)

TITLE_PROFILE = LLMTaskProfile(
    name="conversation_title",
    system_prompt=TITLE_SYSTEM_PROMPT,
    parser=LLMParserType.PLAIN_TEXT,
    allow_markdown=False,
    require_json=False,
    max_tokens=64,
    temperature=0.3,
    timeout_override=20,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text="新的对话",
)


# ── 4. TEST_PLAN_PROFILE — single-pass test plan generation ─────────
#
# TestPlanGeneratorTool uses PromptBuilder + LLMClient.generate in one
# pass (F019 removed the previous SplitGenerator two-pass pipeline).
# TEST_PLAN_PROFILE is registered as the contract for this call site
# so that other modules can reference the same parser/temperature/
# max_tokens settings without re-declaring them.  The tool currently
# drives the call through ``LLMClient.generate`` rather than
# ``generate_with_profile`` — the profile is metadata, not enforced.

TEST_PLAN_OUTLINE_PROFILE = LLMTaskProfile(
    name="test_plan_generation_outline",
    system_prompt=(
        "Generate a compact consistency outline only. Follow the caller's "
        "strict JSON contract; do not generate test-plan chapter content."
    ),
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=1800,
    temperature=0.1,
    timeout_override=45,
    on_parse_failure=LLMParseFailurePolicy.RAISE,
    fallback_text=None,
)


TEST_PLAN_BATCH_PROFILE = LLMTaskProfile(
    name="test_plan_generation_batch",
    system_prompt=(
        "Generate only the explicitly assigned test-plan fields. Follow the "
        "server-derived strict JSON contract exactly; never add wrapper keys, "
        "prose, or Markdown."
    ),
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=8000,
    temperature=0.2,
    timeout_override=90,
    on_parse_failure=LLMParseFailurePolicy.RAISE,
    fallback_text=None,
)


TEST_PLAN_PROFILE = LLMTaskProfile(
    name="test_plan_generation",
    system_prompt="由 PromptBuilder 动态生成，此处仅登记任务契约。",
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    # 2026-07：从 8000 提到 16000。完整中文测试方案响应常达 23k+ 字符
    # （≈ 12k+ tokens），旧值会在中途被截断。代码层由
    # ``_resolve_max_tokens`` 兜底（优先级：cfg.max_tokens → 协议级 16000
    # → provider 默认），这里只是契约/文档。
    max_tokens=16000,
    temperature=0.2,
    on_parse_failure=LLMParseFailurePolicy.RAISE,
    fallback_text=None,
    # 2026-07：单独收紧测试方案生成的超时。DB 默认 120s 偏长，导致
    # 一次失败 + openai SDK 自动 retry 累计 360s。把单次上限降到 90s
    # （正常 30-60s 出结果，留余量），失败时让上层 F023 RetryPolicy
    # 决策是否重试，不再被 SDK retry 拖延。
    timeout_override=90,
)


# Context Engine must compose the generation request, but it must not parse
# this particular response before TestPlanGeneratorTool has a chance to run
# its JSON repair and two-field batch recovery protocol.  RAW_TEXT therefore
# preserves the provider output byte-for-byte for the tool-owned validator.
TEST_PLAN_RAW_PROFILE = LLMTaskProfile(
    name="test_plan_generation_raw",
    system_prompt="",
    parser=LLMParserType.RAW_TEXT,
    allow_markdown=False,
    require_json=False,
    max_tokens=16000,
    temperature=0.2,
    timeout_override=90,
    on_parse_failure=LLMParseFailurePolicy.RAISE,
    fallback_text=None,
)


TEST_PLAN_JSON_REPAIR_RAW_PROFILE = LLMTaskProfile(
    name="test_plan_generation_json_repair_raw",
    system_prompt="",
    parser=LLMParserType.RAW_TEXT,
    allow_markdown=False,
    require_json=False,
    max_tokens=16000,
    temperature=0.0,
    timeout_override=90,
    on_parse_failure=LLMParseFailurePolicy.RAISE,
    fallback_text=None,
)


REQUIREMENT_EVIDENCE_EXTRACT_SYSTEM_PROMPT = (
    "你是需求证据提取器。只处理给定的一个连续来源分块，不补写未提供的事实。"
    "完整提取该分块内所有可测试需求、业务规则、角色、流程、状态、字段、数值、"
    "日期、异常、边界、依赖、排除项、待确认项和相互矛盾之处。保留原始标识符和"
    "精确数值；不要为了简短而合并不同约束。输出简洁的 Markdown 条目，不要输出"
    "代码块，也不要声称看过其他分块。"
)

REQUIREMENT_EVIDENCE_EXTRACT_PROFILE = LLMTaskProfile(
    name="requirement_evidence_extract",
    system_prompt=REQUIREMENT_EVIDENCE_EXTRACT_SYSTEM_PROMPT,
    parser=LLMParserType.PLAIN_TEXT,
    allow_markdown=True,
    require_json=False,
    max_tokens=1800,
    temperature=0.0,
    timeout_override=60,
    on_parse_failure=LLMParseFailurePolicy.RAISE,
    fallback_text=None,
)


# ── 5. SUMMARY_PROFILE — conversation summary compression ────────

SUMMARY_SYSTEM_PROMPT = (
    "你是 TestAgent 的会话摘要器。请把历史对话压缩成结构化摘要。\n\n"
    "【必须保留的信息】\n"
    "1. 用户陈述的具体事实（人名、关系、偏好、数字、日期等）；\n"
    "2. 用户当前目标和已讨论的关键需求；\n"
    "3. 已上传文件及其用途；\n"
    "4. 最近 Agent 任务的状态（进行中/等待确认/已完成/失败）；\n"
    "5. 用户明确表达的偏好或约束；\n"
    "6. 当前未完成事项。\n\n"
    "【格式要求】\n"
    "- 用简洁的条目式记录，每条一行；\n"
    "- 直接记录事实内容，不要用「用户聊了关于 XX 的话题」这类概括；\n"
    "- 例如：记录「小明喜欢小红」，而不是「用户聊了小明的话题」；\n"
    "- 不要输出 Markdown 标题；\n"
    "- 不要包含 API Key、文件路径、内部 ID、完整文件内容；\n"
    "- 不要加入对话中不存在的信息；\n"
    "- 控制在 800 字以内。"
)

SUMMARY_PROFILE = LLMTaskProfile(
    name="conversation_summary",
    system_prompt=SUMMARY_SYSTEM_PROMPT,
    parser=LLMParserType.PLAIN_TEXT,
    allow_markdown=False,
    require_json=False,
    max_tokens=800,
    temperature=0.2,
    # Summary is best-effort background maintenance.  It must release a
    # provider slot promptly when an upstream model stalls so a user's next
    # interactive chat request is never held behind it for minutes.
    timeout_override=30,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text="",
)


# ── 6. PREPARATION_PROFILE — dynamic KB + gap-detection (Phase 2.3) ───
#
# Note: the full system prompt is held by
# ``app.agent_runtime.preparation.prompt.PREPARATION_SYSTEM_PROMPT``.
# This profile only registers the contract: strict JSON, 2048 tokens,
# temperature 0.2, FALLBACK_DEFAULT. The actual system prompt is filled
# in at call time by ``build_preparation_prompt``.

PREPARATION_FALLBACK_JSON = (
    '{"action":"finish","decision_summary":"LLM 解析失败,使用默认方案",'
    '"public_update":"将使用默认方案继续。",'
    '"expected_result":"无","confidence":0.0}'
)

PREPARATION_PROFILE = LLMTaskProfile(
    name="preparation_agent",
    system_prompt="",  # filled by build_preparation_prompt at call time
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=2048,
    temperature=0.2,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=PREPARATION_FALLBACK_JSON,
)


# ── 7. REPAIR_PROFILE — Review-Repair dynamic agent (Phase 2.4) ─────
#
# Note: the full system prompt is held by
# ``app.agent_runtime.repair.prompt.REPAIR_SYSTEM_PROMPT``.
# This profile only registers the contract: strict JSON, 16000 tokens
# (Repair prompt can carry full section JSON), temperature 0.2,
# FALLBACK_DEFAULT. The actual system prompt is filled in at call time
# by ``build_repair_prompt``.

REPAIR_FALLBACK_JSON = (
    '{"action":"finish","decision_summary":"LLM 解析失败,已停止修复",'
    '"public_update":"修复阶段无法继续,已交回主流程根据审查结果处理。",'
    '"expected_result":"fallback to repair failure handling","confidence":0.0}'
)

REPAIR_PROFILE = LLMTaskProfile(
    name="repair_agent",
    system_prompt="",  # filled by build_repair_prompt at call time
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    # 镜像 TEST_PLAN_PROFILE 的 token 上限;Repair prompt 含完整 section
    # JSON 上下文,2048 必然被截断。
    max_tokens=16000,
    temperature=0.2,
    # RepairAgent is a decision step, not document generation. Do not inherit a
    # user-configured 300s model timeout; keep it within the repair loop budget
    # so the UI does not sit at "thinking" for minutes.
    timeout_override=45,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=REPAIR_FALLBACK_JSON,
)


# ── 8. INCREMENTAL_PROFILE — Incremental Task Agent (Phase 2.5) ───
#
# Note: the full system prompt is held by
# ``app.agent_runtime.incremental.prompt.INCREMENTAL_SYSTEM_PROMPT``.
# This profile only registers the contract: strict JSON, 8000 tokens
# (Incremental prompt 含完整 section JSON + last review/format check),
# temperature 0.2, FALLBACK_DEFAULT. The actual system prompt is filled
# in at call time by ``build_incremental_prompt``.

INCREMENTAL_FALLBACK_JSON = (
    '{"action":"finish","decision_summary":"LLM 解析失败,已停止增量任务",'
    '"public_update":"增量任务无法继续,将进入降级流程。",'
    '"expected_result":"fallback to legacy regen","confidence":0.0,'
    '"scope_kind":"modify_section"}'
)

INCREMENTAL_PROFILE = LLMTaskProfile(
    name="incremental_agent",
    system_prompt="",  # filled by build_incremental_prompt at call time
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    # 8000 token:含 section JSON + review/format 历史 + 决策链
    max_tokens=8000,
    temperature=0.2,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=INCREMENTAL_FALLBACK_JSON,
)


# ── Public registry of built-in profiles ───────────────────────────


# ── 9. TOOL_NARRATIVE_COMPOSER — LLM-first Tool 叙事(Phase 2.9B.4) ──
#
# Reuse the user's model configuration.  NarrativeComposer supplies a tagged
# <NARRATIVE> stream contract at call time, so the parser must preserve both
# the wrapper and Markdown rather than treating the response as a title.

TOOL_NARRATIVE_COMPOSER_PROFILE = LLMTaskProfile(
    name="tool_narrative_composer",
    system_prompt="",  # filled by NarrativeComposer at call time
    parser=LLMParserType.MARKDOWN,
    allow_markdown=True,
    require_json=False,
    max_tokens=1024,
    temperature=0.3,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=None,
)

# ── 10. DYNAMIC_AGENT_PLANNER — Hybrid Agent runtime planning ─────

DYNAMIC_AGENT_PLANNER_SYSTEM_PROMPT = (
    "You are the Dynamic Agent planner for TestAgent. Build a minimal structured "
    "runtime plan that satisfies the user's goal with only the supplied approved "
    "capabilities. Return strict JSON only. Do not mention backend class names, "
    "internal prompts, or hidden reasoning. Each step must include step_id, title, "
    "action_type, capability_key, input_refs, depends_on, and success_criteria."
)

DYNAMIC_AGENT_PLANNER_PROFILE = LLMTaskProfile(
    name="dynamic_agent_planner",
    system_prompt=DYNAMIC_AGENT_PLANNER_SYSTEM_PROMPT,
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=1800,
    temperature=0.1,
    on_parse_failure=LLMParseFailurePolicy.RAISE,
    fallback_text=None,
)


# ── 11. TASK_SUMMARY_NARRATIVE_COMPOSER — 最终任务总结(Phase 2.9B.4) ──

TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE = LLMTaskProfile(
    name="task_summary_narrative_composer",
    system_prompt="",  # filled by TaskNarrativeSummaryNode at call time
    parser=LLMParserType.MARKDOWN,
    allow_markdown=True,
    require_json=False,
    max_tokens=1024,
    temperature=0.3,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=None,
)

# ── 11. NARRATIVE_SCHEMA_REPAIR — 叙事结构修复(Phase 2.9B.4) ──

NARRATIVE_SCHEMA_REPAIR_PROFILE = LLMTaskProfile(
    name="narrative_schema_repair",
    system_prompt="",  # filled by NarrativeComposer at call time
    parser=LLMParserType.MARKDOWN,
    allow_markdown=True,
    require_json=False,
    max_tokens=1024,
    temperature=0.2,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=None,
)


# ── 12. MEMORY_EXTRACT_PROFILE — 用户记忆/项目规则自动提取 ─────────
# WP-BE-06/08：Post-turn Context Learning。严格 JSON 输出三类提取结果。
# 输出不含正文/secret；后续由 ContextLearningService 做安全校验。

MEMORY_EXTRACT_SYSTEM_PROMPT = (
    "你是上下文学习器。从用户消息中提取值得长期记住的信息。\n"
    "只提取用户明确表达或强烈暗示的稳定偏好/约束/项目规则。\n"
    "临时任务、一次性指令、对话过程本身 → EPHEMERAL。\n"
    "不要提取助手自己生成的内容。\n"
    "禁止提取密码、Token、API Key、账号密码、Cookie、Private Key。\n"
    "PROJECT_RULE 和 USER_MEMORY 都必须提供 key：规则或偏好的稳定主题标识（小写蛇形，如 "
    "api_version、no_ie、report_chapter、encoding_style）。同一主题的新旧规则 "
    "（如 v3→v4）key 必须相同，以便系统识别为更新而非新规则。\n"
    "输出严格 JSON：\n"
    '{"items":[{"category":"USER_MEMORY|PROJECT_RULE|EPHEMERAL",'
    '"key":"规则或偏好的稳定主题键；同一主题的新旧值必须相同",'
    '"content":"一句话表达的信息",'
    '"reason":"为什么值得记住"}]}\n'
    "不输出任何其他内容。"
)

MEMORY_EXTRACT_PROFILE = LLMTaskProfile(
    name="memory_extract",
    system_prompt=MEMORY_EXTRACT_SYSTEM_PROMPT,
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=1024,
    temperature=0.0,
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text='{"items":[]}',
)


FILE_UNDERSTANDING_SYSTEM_PROMPT = (
    "You classify an uploaded document for TestAgent. Use the provided bounded sample, "
    "not the filename alone. Prefer content evidence such as title, headings, table headers, "
    "statistics, and representative excerpts. Return strict JSON only with keys: "
    "document_kind, summary, semantic_labels, possible_usages, confidence. "
    "Allowed document_kind values are requirements_specification, test_plan_template, "
    "supplemental_reference, unknown. possible_usages may include requirement_source, "
    "output_template, reference_material. Do not copy secrets, personal data, or long text."
)

FILE_UNDERSTANDING_FALLBACK_JSON = (
    '{"document_kind":"unknown","summary":"","semantic_labels":[],'
    '"possible_usages":[],"confidence":0.0}'
)

FILE_UNDERSTANDING_PROFILE = LLMTaskProfile(
    name="file_understanding",
    system_prompt=FILE_UNDERSTANDING_SYSTEM_PROMPT,
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=1000,
    temperature=0.0,
    timeout_override=45,
    output_schema={
        "type": "object",
        "required": [
            "document_kind",
            "summary",
            "semantic_labels",
            "possible_usages",
            "confidence",
        ],
        "properties": {
            "document_kind": {"type": "string"},
            "summary": {"type": "string"},
            "semantic_labels": {"type": "array", "items": {"type": "string"}},
            "possible_usages": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
    },
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=FILE_UNDERSTANDING_FALLBACK_JSON,
)


ATTACHMENT_BINDING_SYSTEM_PROMPT = (
    "You resolve uploaded attachments into task binding roles. Use only the supplied "
    "metadata, user message, safe summaries, possible usages, and task schema. Do not "
    "request or infer from full document text. Return strict JSON with status and bindings. "
    "Allowed statuses are RESOLVED and CLARIFICATION_REQUIRED. Each binding must include "
    "file_public_id, binding_role, and confidence."
)

ATTACHMENT_BINDING_FALLBACK_JSON = (
    '{"status":"CLARIFICATION_REQUIRED","bindings":[],"reason":"binding_parse_failed"}'
)

ATTACHMENT_BINDING_PROFILE = LLMTaskProfile(
    name="attachment_binding",
    system_prompt=ATTACHMENT_BINDING_SYSTEM_PROMPT,
    parser=LLMParserType.JSON_STRICT,
    allow_markdown=False,
    require_json=True,
    max_tokens=1200,
    temperature=0.0,
    timeout_override=45,
    output_schema={
        "type": "object",
        "required": ["status", "bindings"],
        "properties": {
            "status": {"type": "string"},
            "reason": {"type": "string"},
            "bindings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["file_public_id", "binding_role", "confidence"],
                    "properties": {
                        "file_public_id": {"type": "string"},
                        "binding_role": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                },
            },
        },
    },
    on_parse_failure=LLMParseFailurePolicy.FALLBACK_DEFAULT,
    fallback_text=ATTACHMENT_BINDING_FALLBACK_JSON,
)


BUILTIN_PROFILES: dict[str, LLMTaskProfile] = {
    p.name: p
    for p in (
        CHAT_PROFILE,
        INTENT_PROFILE,
        TITLE_PROFILE,
        TEST_PLAN_OUTLINE_PROFILE,
        TEST_PLAN_BATCH_PROFILE,
        TEST_PLAN_PROFILE,
        TEST_PLAN_RAW_PROFILE,
        TEST_PLAN_JSON_REPAIR_RAW_PROFILE,
        REQUIREMENT_EVIDENCE_EXTRACT_PROFILE,
        SUMMARY_PROFILE,
        PREPARATION_PROFILE,
        REPAIR_PROFILE,
        INCREMENTAL_PROFILE,
        TOOL_NARRATIVE_COMPOSER_PROFILE,
        DYNAMIC_AGENT_PLANNER_PROFILE,
        TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE,
        NARRATIVE_SCHEMA_REPAIR_PROFILE,
        MEMORY_EXTRACT_PROFILE,
        FILE_UNDERSTANDING_PROFILE,
        ATTACHMENT_BINDING_PROFILE,
    )
}


def get_builtin_profile(name: str) -> LLMTaskProfile:
    """Return a built-in profile by name (case-insensitive).

    Raises ``LLMProfileConfigError`` if the name is unknown.
    """
    for key, profile in BUILTIN_PROFILES.items():
        if key.lower() == name.lower():
            return profile
    from app.llm.errors import LLMProfileConfigError

    raise LLMProfileConfigError(
        f"unknown built-in profile: {name!r}; "
        f"available: {sorted(BUILTIN_PROFILES)}"
    )
