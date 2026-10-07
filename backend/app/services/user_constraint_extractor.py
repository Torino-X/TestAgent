"""F022 — User constraint extractor.

Reads the user-supplied prompt (e.g. "测试设备章节保留原文，不使用 AI 生成")
and extracts per-section handling intents that override the template's
default suggestions.

Pipeline:
  1. LLM call (low temperature, strict JSON output)
  2. JSON parse + structural validation
  3. Section-title matching (exact → substring)
  4. Action validation against the allowed enum

Failure modes all return ``{}`` so callers can safely treat the
absence of constraints as "no user override" — the orchestrator must
NEVER crash because the user wrote a vague sentence.

Public surface:

  * :class:`UserConstraintExtractor` — async service
  * :func:`match_section_title` — pure helper used by tests
  * :func:`VALID_ACTIONS` — frozenset of allowed actions
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Mapping, Optional

from app.common.json_utils import extract_json_from_llm_response
from app.llm.task_profiles import LLMParserType, LLMTaskProfile
from app.services.settings_service import LLMConfigProvider

logger = logging.getLogger(__name__)

# Compatibility seam for old tests that assert a legacy client is never used.
# It is deliberately not an LLM client and is never read by production code.
LLMClient = None

# Action enum — keep in sync with SectionSuggestionTool's
# ``_MODE_TO_ACTION`` mapping (ai_generate / keep_template / manual_fill)
VALID_ACTIONS: frozenset[str] = frozenset({
    "ai_generate",
    "keep_template",
    "manual_fill",
})

# Default sentinel for actions the LLM hallucinates outside the enum.
# We coerce to ``ai_generate`` (the safe default — explicit user intent
# "don't touch this chapter" is preserved elsewhere via keep_template /
# manual_fill, anything unknown should fall back to the recommendation).
_UNKNOWN_ACTION_FALLBACK = "ai_generate"

_ACTION_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "keep_template",
        (
            "保留模板原文",
            "保留模板",
            "保留原文",
            "保持原文",
            "使用模板原文",
            "不使用AI生成",
            "不使用ai生成",
            "不用AI生成",
            "不用ai生成",
            "不要AI生成",
            "不要ai生成",
            "不生成",
            "不要生成",
        ),
        "用户要求保留原文",
    ),
    (
        "manual_fill",
        (
            "手动填写",
            "人工填写",
            "手动填",
            "人工填",
            "手动补充",
            "人工补充",
            "自己填写",
            "用户填写",
        ),
        "用户要求手动填写",
    ),
    (
        "ai_generate",
        (
            "AI生成",
            "ai生成",
            "由AI生成",
            "由ai生成",
            "让AI生成",
            "让ai生成",
            "使用AI生成",
            "使用ai生成",
        ),
        "用户要求 AI 生成",
    ),
)

_CLAUSE_BOUNDARY_RE = re.compile(r"[。；;\n\r!?！？]")
_ACTION_SEPARATOR_RE = re.compile(r"[，,]")
_QUOTE_RE = re.compile(r"[“\"'‘]([^”\"'’]+)[”\"'’]")


# ── Prompt ────────────────────────────────────────────────────────────

_CONSTRAINT_SYSTEM_PROMPT = (
    "你是测试方案章节处理意图识别助手。"
    "请仔细阅读用户的提示词，识别其中提到的章节以及对应的处理意图。"
    "只能输出一个严格合法的 JSON 对象，禁止任何解释、Markdown 代码块或多余文字。"
)

_CONSTRAINT_USER_PROMPT_TEMPLATE = """\
【用户提示词】
{user_prompt}

【模板章节标题列表】
{titles_block}

【可选 action 枚举】
- "ai_generate"     — 让 AI 根据需求文档生成该章节
- "keep_template"   — 保留模板原文，不使用 AI 生成
- "manual_fill"     — 由用户手动填写该章节

请识别用户提示词中明确提到的章节标题，并给出对应的 action。
要求：
1. 章节标题必须从上面的"模板章节标题列表"中精确选择（可去掉编号前缀，例如"3.2 测试设备"应匹配到"测试设备"）；
2. 如果用户没明确提到某个章节，不要在 constraints 里输出它；
3. 如果用户提示词完全没有提到任何章节，返回 {{"constraints": []}}；
4. action 必须是上述三个枚举值之一；
5. reason 字段请用一句话概括用户的原话意图，例如"用户要求保留原文"或"用户要求手动填写"。

输出 JSON 格式（严格遵守，不要多余字段）：
{{"constraints": [{{"section_title": "测试设备", "action": "keep_template", "reason": "用户要求保留原文"}}, ...]}}
"""


# ── CE-04 §四：统一 ContextInvokerBridge 迁移契约 ──────────────────
# section_suggest 属于 preparation 子图（MIG_PREPARATION flag 门控）。
# 走 bridge 时用既有 SYSTEM_PROMPT 构造 LLMTaskProfile；call_site 需命中
# context_engine/profiles/registry.py 中已注册的 test_plan.section_suggest 映射。
_SECTION_SUGGEST_CALL_SITE = "test_plan.section_suggest"
_SECTION_SUGGEST_LLM_PROFILE = LLMTaskProfile(
    name="section.suggest.v1",
    system_prompt=_CONSTRAINT_SYSTEM_PROMPT,
    parser=LLMParserType.JSON_STRICT,
    require_json=True,
    allow_markdown=False,
)


# ── Pure helpers ──────────────────────────────────────────────────────


def _strip_numeric_prefix(title: str) -> str:
    """Drop leading numeric prefix like "3.2 " or "4.1.1 " from a title.

    Used during LLM output normalisation so the model can match
    "3.2 测试设备" → "测试设备" without us baking strict matching rules
    into the prompt.
    """
    import re

    return re.sub(r"^\s*\d+(?:\.\d+)*\s*\.?\s*", "", title).strip()


def match_section_title(
    llm_title: str,
    section_titles: List[str],
) -> Optional[str]:
    """Match an LLM-returned section title to the canonical list.

    Three-tier fallback:
      1. Exact match (after stripping numeric prefix).
      2. The canonical title appears as a substring of the LLM title.
      3. The LLM title (post-strip) appears as a substring of the
         canonical title — handles "测试设备" → "3.2 测试设备清单".

    Returns the canonical title on success, ``None`` otherwise.
    """
    if not llm_title:
        return None

    normalised = _strip_numeric_prefix(llm_title.strip())
    if not normalised:
        return None

    # Tier 1: exact (after strip)
    for canon in section_titles:
        if not isinstance(canon, str) or not canon.strip():
            continue
        if _strip_numeric_prefix(canon) == normalised:
            return canon

    # Tier 2: canonical ⊆ LLM
    for canon in section_titles:
        if not isinstance(canon, str) or not canon.strip():
            continue
        if normalised in canon:
            return canon

    # Tier 3: LLM ⊆ canonical
    for canon in section_titles:
        if not isinstance(canon, str) or not canon.strip():
            continue
        if canon in normalised:
            return canon

    return None


def _explicit_constraints_from_prompt(
    user_prompt: str,
    section_titles: List[str],
) -> Dict[str, Dict[str, str]]:
    """Extract obvious section-action constraints without an LLM call.

    This handles high-confidence user commands such as:
    ``其中的“项目概述”，“测试目标”章节保留原文``.

    LLM extraction remains the primary path. This helper is only used
    as a local fallback when the LLM/bridge path is unavailable, fails,
    or returns no usable constraints.
    """
    prompt = (user_prompt or "").strip()
    if not prompt or not section_titles:
        return {}

    spans = _find_action_spans(prompt)
    if not spans:
        return {}

    result: Dict[str, Dict[str, str]] = {}
    for start, end, action, reason in spans:
        segment = _clause_containing(prompt, start, end, spans)
        matched_titles = _mentioned_section_titles(segment, section_titles)
        for title in matched_titles:
            result[title] = {
                "action": action,
                "reason": reason,
            }

    if result:
        logger.info(
            "UserConstraintExtractor: 规则抽取到 %d 条用户约束 | actions=%s",
            len(result),
            {k: v["action"] for k, v in result.items()},
        )
    return result


def _find_action_spans(prompt: str) -> list[tuple[int, int, str, str]]:
    spans: list[tuple[int, int, str, str]] = []
    occupied: list[tuple[int, int]] = []

    for action, keywords, reason in _ACTION_RULES:
        for keyword in sorted(keywords, key=len, reverse=True):
            for match in re.finditer(re.escape(keyword), prompt, flags=re.IGNORECASE):
                start, end = match.span()
                if action == "ai_generate" and _is_negated_ai_action(prompt, start):
                    continue
                if any(not (end <= used_start or start >= used_end) for used_start, used_end in occupied):
                    continue
                spans.append((start, end, action, reason))
                occupied.append((start, end))

    return sorted(spans, key=lambda item: item[0])


def _is_negated_ai_action(prompt: str, action_start: int) -> bool:
    prefix = prompt[max(0, action_start - 6):action_start]
    return any(token in prefix for token in ("不", "不要", "不用", "无需", "别"))


def _clause_containing(
    prompt: str,
    start: int,
    end: int,
    spans: list[tuple[int, int, str, str]],
) -> str:
    left = 0
    for match in _CLAUSE_BOUNDARY_RE.finditer(prompt, 0, start):
        left = match.end()

    right_match = _CLAUSE_BOUNDARY_RE.search(prompt, end)
    right = right_match.start() if right_match else len(prompt)

    previous_spans = [span for span in spans if span[1] <= start]
    if previous_spans:
        previous_end = previous_spans[-1][1]
        for match in _ACTION_SEPARATOR_RE.finditer(prompt, previous_end, start):
            left = max(left, match.end())

    next_spans = [span for span in spans if span[0] >= end]
    if next_spans:
        next_start = next_spans[0][0]
        for match in _ACTION_SEPARATOR_RE.finditer(prompt, end, next_start):
            right = min(right, match.start())
            break

    return prompt[left:right].strip()


def _mentioned_section_titles(
    segment: str,
    section_titles: List[str],
) -> list[str]:
    matched: list[str] = []

    def add(raw_title: str) -> None:
        canon = match_section_title(raw_title, section_titles)
        if canon is not None and canon not in matched:
            matched.append(canon)

    for quoted in _QUOTE_RE.findall(segment):
        add(quoted.strip())

    for canon in section_titles:
        if not isinstance(canon, str) or not canon.strip():
            continue
        stripped = _strip_numeric_prefix(canon)
        if not stripped:
            continue
        if canon in segment or stripped in segment:
            add(stripped)

    return matched


def _coerce_action(raw: Any) -> Optional[str]:
    """Return a valid action string, or ``None`` if totally unusable."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    # Common LLM aliases — mapped to the canonical enum.
    aliases = {
        "ai": "ai_generate",
        "generate": "ai_generate",
        "ai生成": "ai_generate",
        "让ai生成": "ai_generate",
        "keep": "keep_template",
        "保留原文": "keep_template",
        "保留模板原文": "keep_template",
        "不生成": "keep_template",
        "manual": "manual_fill",
        "手动": "manual_fill",
        "手动填写": "manual_fill",
        "自己填": "manual_fill",
    }
    if cleaned in VALID_ACTIONS:
        return cleaned
    if cleaned in aliases:
        return aliases[cleaned]
    return None


# ── Service ───────────────────────────────────────────────────────────


class UserConstraintExtractor:
    """Extract per-section handling intents from the user's prompt.

    Single public method :meth:`extract` — call once per SectionSuggestion
    phase.  Accepts a pre-resolved :class:`LLMConfigProvider` (so tests
    can inject a mock without touching the DB).
    """

    def __init__(
        self,
        llm_provider: Any = None,
        context_llm_invoker: Any = None,
        user_internal_id: int = 0,
        task_flag_resolver: Any = None,
        task_id: int | str | None = None,
        conversation_id: int | None = None,
        runtime_context: Any = None,
    ) -> None:
        # Kept as a constructor argument for callers compiled against the old
        # service API.  It is intentionally not used to construct a raw LLM.
        del llm_provider
        # CE-only: every model-backed extraction goes through this bridge.
        self._context_llm_invoker = context_llm_invoker
        # bridge 路径的 snapshot 归属需要内部 user id。
        self._user_internal_id = user_internal_id
        # ``test_plan.section_suggest`` has a required TASK_STATE section.
        # Keep the request identity from the active graph runtime so the
        # bridge can build that section instead of silently degrading with
        # ``context.source.no_task``.
        self._task_id = task_id
        self._conversation_id = conversation_id
        self._runtime_context = runtime_context
        # CE-05 WP-2：任务级 Flag Resolver（冻结 Manifest）；None → fail closed。
        self._task_flag_resolver = task_flag_resolver

    async def extract(
        self,
        user_prompt: str,
        section_titles: List[str],
    ) -> Dict[str, Dict[str, str]]:
        """Return ``{canonical_section_title: {"action": str, "reason": str}}``.

        Empty dict means "no user constraint" — callers should fall back
        to the template's default suggestions.
        """
        if not user_prompt or not user_prompt.strip():
            logger.info("UserConstraintExtractor: 用户提示词为空，跳过用户约束抽取")
            return {}
        if not section_titles:
            logger.info("UserConstraintExtractor: 模板章节标题为空，跳过用户约束抽取")
            return {}

        prompt = user_prompt.strip()
        logger.info(
            "UserConstraintExtractor: 开始用户约束抽取 | prompt_len=%d | section_count=%d",
            len(prompt),
            len(section_titles),
        )

        if self._bridge_available():
            logger.info(
                "UserConstraintExtractor: 使用 ContextInvokerBridge 抽取用户约束 | call_site=%s",
                _SECTION_SUGGEST_CALL_SITE,
            )
            raw = await self._call_llm_via_bridge(
                self._build_user_content(prompt, section_titles),
            )
            result = self._parse_llm_result_or_empty(raw, section_titles, source="bridge")
            if result:
                return result
        else:
            logger.info(
                "UserConstraintExtractor: Context Engine unavailable; using deterministic rule fallback only",
            )

        deterministic = _explicit_constraints_from_prompt(prompt, section_titles)
        if deterministic:
            logger.info(
                "UserConstraintExtractor: CE extraction produced no valid constraint; using rule fallback | constraints=%s",
                deterministic,
            )
            return deterministic

        logger.info("UserConstraintExtractor: 未抽取到用户章节约束，返回空约束")
        return {}

    def _parse_llm_result_or_empty(
        self,
        raw: Optional[str],
        section_titles: List[str],
        *,
        source: str,
    ) -> Dict[str, Dict[str, str]]:
        if raw is None:
            logger.info(
                "UserConstraintExtractor: %s LLM 未返回可解析内容",
                source,
            )
            return {}
        result = self._parse_and_match(raw, section_titles, source=source)
        if result:
            logger.info(
                "UserConstraintExtractor: %s LLM 抽取成功 | constraints=%s",
                source,
                result,
            )
        else:
            logger.info(
                "UserConstraintExtractor: %s LLM 未抽取到有效用户约束",
                source,
            )
        return result

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _build_user_content(
        user_prompt: str,
        section_titles: List[str],
    ) -> str:
        titles_block = "\n".join(
            f"- {idx + 1}. {title}"
            for idx, title in enumerate(section_titles)
        )
        return _CONSTRAINT_USER_PROMPT_TEMPLATE.format(
            user_prompt=user_prompt.strip(),
            titles_block=titles_block,
        )

    def _bridge_available(self) -> bool:
        """MIG_PREPARATION=true 且 bridge 可用才走桥接路径。"""
        from app.context_engine.feature_flags import is_agent_context_migration_enabled

        # CE-05 WP-2：任务路径经 task_flag_resolver 读 MIG_PREPARATION（冻结
        # Manifest）；resolver 缺失/损坏 → fail closed。
        mig_preparation = is_agent_context_migration_enabled(
            self._task_flag_resolver, "MIG_PREPARATION"
        )
        if not mig_preparation:
            return False
        bridge = self._context_llm_invoker
        if bridge is None or not getattr(bridge, "available", False):
            logger.warning(
                "UserConstraintExtractor: MIG_PREPARATION=true 但 Invoker 不可用，跳过约束抽取",
            )
            return False
        return True

    async def _call_llm_via_bridge(self, user_content: str) -> Optional[str]:
        """经 ContextInvokerBridge.generate 调用；失败返回 None。"""
        bridge = self._context_llm_invoker
        try:
            bres = await bridge.generate(
                user_id=self._user_internal_id,
                call_site=_SECTION_SUGGEST_CALL_SITE,
                llm_task_profile=_SECTION_SUGGEST_LLM_PROFILE,
                current_goal=user_content,
                output_contract="json",
                user_content=user_content,
                conversation_id=self._conversation_id,
                task_id=self._task_id,
                runtime_context=self._runtime_context,
            )
        except Exception as exc:  # noqa: BLE001 — 失败保持跳过
            logger.warning(
                "UserConstraintExtractor: Bridge 调用异常，跳过约束抽取 | err=%s",
                str(exc)[:200],
            )
            return None
        if bres is None or bres.value is None:
            logger.warning(
                "UserConstraintExtractor: Bridge 无返回（失败），跳过约束抽取",
            )
            return None
        return str(bres.value)

    def _parse_and_match(
        self,
        raw: str,
        section_titles: List[str],
        *,
        source: str = "llm",
    ) -> Dict[str, Dict[str, str]]:
        try:
            data = extract_json_from_llm_response(raw)
        except ValueError as exc:
            logger.warning(
                "UserConstraintExtractor: %s JSON 解析失败 | err=%s | raw=%s",
                source, str(exc)[:200], raw[:200],
            )
            return {}
        if not isinstance(data, Mapping):
            logger.warning(
                "UserConstraintExtractor: %s 顶层不是对象，跳过 | raw=%s",
                source, raw[:200],
            )
            return {}

        constraints = data.get("constraints")
        if not isinstance(constraints, list):
            logger.info(
                "UserConstraintExtractor: %s constraints 字段缺失或非数组",
                source,
            )
            return {}
        logger.info(
            "UserConstraintExtractor: %s LLM 返回 constraints_count=%d",
            source,
            len(constraints),
        )

        result: Dict[str, Dict[str, str]] = {}
        for item in constraints:
            if not isinstance(item, Mapping):
                continue
            llm_title = item.get("section_title")
            raw_action = item.get("action")
            reason = item.get("reason", "")

            action = _coerce_action(raw_action)
            if action is None:
                logger.info(
                    "UserConstraintExtractor: %s 非法 action 跳过 | "
                    "title=%s | action=%s",
                    source, llm_title, raw_action,
                )
                continue

            canon = match_section_title(str(llm_title or ""), section_titles)
            if canon is None:
                logger.info(
                    "UserConstraintExtractor: %s 章节无法匹配模板，跳过 | "
                    "title=%s | known=%s",
                    source, llm_title, section_titles,
                )
                continue

            # Last-write-wins if the model emits duplicate canonical titles.
            result[canon] = {
                "action": action,
                "reason": str(reason or "").strip(),
            }

        if result:
            logger.info(
                "UserConstraintExtractor: %s 解析匹配到 %d 条用户约束 | actions=%s",
                source, len(result), {k: v["action"] for k, v in result.items()},
            )
        elif constraints:
            logger.info(
                "UserConstraintExtractor: %s LLM 返回了约束但全部无效或无法匹配",
                source,
            )
        return result

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (F022 user prompt 解析 → 章节策略覆盖):
#
#   链路:
#     suggest_sections_node 调用 SectionSuggestionTool
#       → SectionSuggestionTool 内部调 UserConstraintExtractor.extract(prompt)
#         → LLM 解析用户的章节策略约束("测试设备保留原文,不使用 AI 生成")
#         → 返回 {"<section_id>": "keep_template" / "ai_generate" / ...}
#       → 写回 overrides 在 section_suggestions 中标 constraint_source='user'
#
# 关键约束(供开发者速查):
#   - 用户没提到的章节保留模板默认值,不受本模块影响;
#   - LLM 输出 schema 失败 → 整体兜底空 dict,等于用户没说约束;
#   - constraint_source 在前端展示"用户指定"小徽章,别忘了回传;
#   - 与 SectionSuggestionTool 是 1:N 调用关系,而不是平级。
