"""Prompt builder with three-anchor strategy — adapted from legacy prompt_builder.py.

Implements the three-anchor rule strategy to combat "Lost in the Middle":
1. **Beginning anchor** — condensed rules, preset format expectations
2. **Middle anchor** — rule reminder after the requirement document
3. **End anchor** — complete detailed rules, final reinforcement

Includes Two-Pass (outline + batch) generation methods for split generation.

Equivalent migration: all capabilities preserved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from app.common.prompt_loader import _find_rules_dir

logger = logging.getLogger(__name__)


class PromptBuilder:
    """Build prompts using the three-anchor strategy.

    System rules are loaded from the ``templates/system_rules/`` directory
    at construction time.
    """

    def __init__(self, rules_dir: Path | None = None):
        if rules_dir is None:
            rules_dir = _find_rules_dir()
        self.rules_dir = Path(rules_dir)
        self._anchor_beginning = self._load_rule_file("anchor_beginning.md")
        self._anchor_middle = self._load_rule_file("anchor_middle.md")
        self._full_rules_template = self._load_rule_file("full_rules.md")

    # ── Main (full-generation) prompt ──────────────────────────────

    def build(
        self,
        user_prompt: str,
        requirement_text: str,
        template_structure: Dict[str, List[str]],
        template_generation_config: Dict[str, object] | None = None,
        knowledge_context: Dict[str, object] | None = None,
    ) -> str:
        """Build the full-generation prompt (single-pass).

        Args:
            user_prompt: User-supplied supplementary instructions.
            requirement_text: Full requirement document text.
            template_structure: ``{"headings": [...], "table_fields": [...]}``.
            template_generation_config: Output of
                ``TemplatePromptPreviewService.build_generation_config()``.

        Returns:
            Complete prompt string ready to send to the LLM.
        """
        headings = (
            "\n".join(f"- {item}" for item in template_structure.get("headings", []))
            or "未识别到标题结构"
        )
        tables = (
            "\n".join(f"- {item}" for item in template_structure.get("table_fields", []))
            or "未识别到表格字段"
        )
        generation_rules = self._build_generation_rules(template_generation_config)
        full_rules = self._full_rules_template.replace("{generation_rules}", generation_rules)
        knowledge_block = self._build_knowledge_context_block(knowledge_context)

        return (
            f"{self._anchor_beginning}\n\n"
            f"{user_prompt.strip()}\n\n"
            f"【项目需求文档内容】\n{requirement_text.strip()}\n\n"
            f"【测试方案模板标题结构】\n{headings}\n\n"
            f"【测试方案模板表格字段】\n{tables}\n\n"
            f"{knowledge_block}"
            f"{self._anchor_middle}\n\n"
            f"{full_rules}"
        )

    # ── Two-Pass prompts ───────────────────────────────────────────

    def build_outline_prompt(
        self,
        user_prompt: str,
        requirement_text: str,
        template_structure: Dict[str, List[str]],
        ai_fields: List[Dict[str, object]],
    ) -> str:
        """Build consistency outline prompt (Pass 1).

        Produces ~500-token JSON with terminology glossary, cross-cutting
        decisions, and one-sentence summaries per AI field.
        """
        headings = (
            "\n".join(f"- {item}" for item in template_structure.get("headings", []))
            or "未识别到标题结构"
        )
        field_list = self._format_ai_fields_summary(ai_fields)

        return (
            f"{user_prompt.strip()}\n\n"
            '【任务目标】\n'
            '你只需要生成一份“一致性大纲”，不需要生成任何测试方案正文。\n'
            '大纲用于指导后续多个独立批次对同一个测试方案的各章节分别生成，保证术语统一、逻辑一致、不矛盾不重复。\n\n'
            f"【项目需求文档内容】\n{requirement_text.strip()}\n\n"
            f"【测试方案模板全部标题结构】\n{headings}\n\n"
            f"【AI 需要生成的全部章节列表】\n{field_list}\n\n"
            '【输出要求】\n'
            '输出一个严格的 JSON 对象，约 500 tokens，格式如下：\n'
            '{\n'
            '  "terminology": {"核心术语1": "统一定义", "核心术语2": "统一定义"},\n'
            '  "cross_cutting": ["跨章节关键决策1", "跨章节关键决策2"],\n'
            '  "chapter_summaries": {\n'
            '    "field_name_1": "该章节的一句话内容摘要",\n'
            '    "field_name_2": "该章节的一句话内容摘要"\n'
            '  }\n'
            '}\n\n'
            '要求：\n'
            '1. terminology：从需求文档和章节标题中提取 3-8 个核心术语并统一定义，后续所有批次必须使用相同术语。\n'
            '2. cross_cutting：列出 2-5 条贯穿多个章节的关键决策（如测试范围边界、风险优先级、策略选择），保证各批次不矛盾。\n'
            '3. chapter_summaries：为每个 AI 生成字段写一句话摘要，描述该章节应包含的核心内容，key 必须与上方'
            '“AI 需要生成的全部章节列表”中的 field_name 完全一致。\n'
            '4. 只能输出一个严格合法的 JSON 对象，不要 Markdown 围栏，不要解释说明。'
        )

    def build_batch_prompt(
        self,
        user_prompt: str,
        requirement_text: str,
        template_structure: Dict[str, List[str]],
        batch_fields: List[Dict[str, object]],
        outline_json: Dict[str, Any],
        generation_rules: str,
    ) -> str:
        """Build a single-batch generation prompt (Pass 2).

        Only includes fields assigned to this batch, carrying the
        consistency outline as context.
        """
        headings = (
            "\n".join(f"- {item}" for item in template_structure.get("headings", []))
            or "未识别到标题结构"
        )
        tables = (
            "\n".join(f"- {item}" for item in template_structure.get("table_fields", []))
            or "未识别到表格字段"
        )

        batch_field_lines = self._format_ai_fields_detail(batch_fields)

        terminology = json.dumps(
            outline_json.get("terminology", {}), ensure_ascii=False, indent=2
        )
        cross_cutting = "\n".join(
            f"- {item}" for item in outline_json.get("cross_cutting", [])
        )
        chapter_summaries = outline_json.get("chapter_summaries", {})

        related_summaries = "\n".join(
            f"- {name}: {summary}"
            for name, summary in chapter_summaries.items()
            if isinstance(summary, str)
        )

        return (
            f"{self._anchor_beginning}\n\n"
            f"{user_prompt.strip()}\n\n"
            f"【任务目标】\n"
            f"你只负责为测试方案模板中本批次允许生成的章节生成高质量文本内容。\n"
            f"程序会负责把 JSON 内容填充回 Word 模板，并保留模板原有样式。\n"
            f"你的输出会被程序按字段直接写入 Word 模板，因此内容必须专业、具体、可落地。\n\n"
            f"【项目需求文档内容】\n{requirement_text.strip()}\n\n"
            f"【测试方案模板标题结构】\n{headings}\n\n"
            f"【测试方案模板表格字段】\n{tables}\n\n"
            f"{self._anchor_middle}\n\n"
            f"【本批次生成字段】（你只能生成以下字段）\n{batch_field_lines}\n\n"
            f"【一致性约束】\n"
            f"你只能生成本批次字段列表中列出的字段。\n"
            f"以下是你必须遵守的跨章节约定（来自一致性大纲）：\n"
            f"- 核心术语统一定义：\n{terminology}\n"
            f"- 跨章节关键决策：\n{cross_cutting}\n"
            f"- 全部章节内容摘要（了解相邻章节内容，避免矛盾和重复）：\n{related_summaries}\n"
            f"请确保你的输出与这些约定一致，不要与相邻章节矛盾或重复。\n\n"
            f"{generation_rules}\n\n"
            f"【最终输出硬性要求】\n"
            f"1. 只能输出一个严格合法的 JSON 对象。\n"
            f"2. 不要输出 Markdown 代码块，不要输出解释说明，不要输出前后缀文字。\n"
            f"3. JSON 顶层字段必须与本批次字段列表完全一致，不能缺少字段，也不能增加字段。\n"
            f"4. 每个字段的内容必须可直接填入对应模板章节，文本要完整、专业、贴合需求文档。\n"
            f"5. 如果章节适合列表或表格，请用 JSON 数组或对象表达，不要使用 Markdown 表格。\n"
            '6. 不允许出现“略”“待补充”“根据实际情况填写”“xxx”“...”等占位内容。\n'
            '7. 不允许编造需求文档未提供的公司名称、人员姓名、真实设备型号、审批结论或固定资产信息。\n'
            '8. JSON 的所有内容值严禁出现“【AI生成】”“【保留原文】”“【手动编辑】”或类似状态标签。'
        )

    # ── Generation rules block ─────────────────────────────────────

    def _build_knowledge_context_block(self, context: Dict[str, object] | None) -> str:
        if not context:
            return ""
        degraded = bool(context.get("degraded") or context.get("disabled_by_config"))
        hit_count = int(context.get("hit_count") or 0)
        query = str(context.get("query") or "").strip()
        error_code = str(context.get("error_code") or context.get("skip_reason") or "").strip()
        error_message = str(context.get("error_message") or "").strip()
        header = [
            "【公司知识库检索结果】",
            f"- 查询词: {query or '未提供'}",
            f"- 检索状态: {'降级/不可用' if degraded else '已完成'}",
            f"- 命中数量: {hit_count}",
            "- 使用规则: 知识库内容只作为测试方法、规范和历史经验补充，不得覆盖需求文档与模板结构。",
        ]
        if error_code or error_message:
            header.append(f"- 降级原因: {error_code} {error_message}".strip())

        snippets: list[str] = []
        raw_hits = context.get("hits")
        if not isinstance(raw_hits, list) or not raw_hits:
            raw_hits = context.get("similar_projects")
        if isinstance(raw_hits, list):
            for idx, item in enumerate(raw_hits[:5], start=1):
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("doc_name") or item.get("doc_id") or f"hit-{idx}")
                    snippet = str(item.get("snippet") or item.get("content") or "").strip()
                    score = item.get("similarity") or item.get("score")
                    score_text = f", score={score}" if score is not None else ""
                    snippets.append(f"{idx}. {name}{score_text}: {snippet[:500]}")
                elif item:
                    snippets.append(f"{idx}. {str(item)[:500]}")
        if snippets:
            header.append("- 命中片段:")
            header.extend(snippets)
        return "\n".join(header).strip() + "\n\n"


    def _build_generation_rules(self, config: Dict[str, object] | None) -> str:
        """Build the ``{generation_rules}`` placeholder content for ``full_rules.md``."""
        if not config:
            return "【模板章节生成范围】\n未配置章节级生成范围，请按完整模板结构生成。"

        ai_fields = config.get("ai_fields", [])
        keep_sections = config.get("keep_sections", [])
        manual_sections = config.get("manual_sections", [])
        json_schema_preview = str(config.get("json_schema_preview", "")).strip()

        if isinstance(ai_fields, list) and ai_fields:
            ai_lines = []
            for item in ai_fields:
                if isinstance(item, dict):
                    section_id = item.get("section_id", "")
                    level = item.get("level", "")
                    table_schemas = item.get("table_schemas", [])
                    table_meta = ""
                    if isinstance(table_schemas, list) and table_schemas:
                        if len(table_schemas) == 1:
                            headers = (
                                table_schemas[0].get("headers", [])
                                if isinstance(table_schemas[0], dict)
                                else []
                            )
                            if headers:
                                complex_hint = (
                                    "；该表格存在复杂格式，必须严格使用模板表头，不要新增列"
                                    if table_schemas[0].get("complex")
                                    else ""
                                )
                                table_meta = (
                                    f"；该章节包含模板表格，字段内容必须返回对象数组，"
                                    f"每个对象的键必须且只能使用这些表头："
                                    f"{'、'.join(str(h) for h in headers)}{complex_hint}"
                                )
                        else:
                            table_descriptions = []
                            for idx, schema in enumerate(table_schemas):
                                if isinstance(schema, dict):
                                    headers = schema.get("headers", [])
                                    if headers:
                                        complex_hint = (
                                            "；该表格存在复杂格式，必须严格使用模板表头，不要新增列"
                                            if schema.get("complex")
                                            else ""
                                        )
                                        table_descriptions.append(
                                            f"表格{idx+1}表头：{'、'.join(str(h) for h in headers)}{complex_hint}"
                                        )
                            if table_descriptions:
                                table_meta = (
                                    f"；该章节包含 {len(table_schemas)} 个模板表格，字段内容必须返回二维数组，"
                                    f"外层数组的每个元素对应一个表格，内层数组是该表格的数据行。"
                                    f"{'；'.join(table_descriptions)}"
                                )
                    else:
                        table_meta = (
                            "；该模板章节没有表格占位，字段内容只能返回纯文本或字符串数组，"
                            "禁止返回对象数组、嵌套数组或任何表格数据"
                        )
                    description = str(item.get("description", "")).strip()
                    desc_hint = f"\n  模板章节说明：{description}" if description else ""
                    meta = (
                        f"；模板位置 {section_id}；层级 {level}{table_meta}"
                        if section_id
                        else table_meta
                    )
                    ai_lines.append(
                        f"- {item.get('field')}：{item.get('title')}{desc_hint}{meta}"
                    )
        else:
            ai_lines = ["- 无，当前没有需要 AI 生成的章节，此时只能返回 {}"]

        if isinstance(keep_sections, list) and keep_sections:
            keep_lines = [f"- {title}" for title in keep_sections]
        else:
            keep_lines = ["- 无"]

        if isinstance(manual_sections, list) and manual_sections:
            manual_lines = [f"- {title}" for title in manual_sections]
        else:
            manual_lines = ["- 无"]

        return (
            f"【模板章节生成范围】\n"
            f"只允许生成以下 JSON 字段：\n"
            f"{chr(10).join(ai_lines)}\n\n"
            f"以下章节必须保留测试方案模板原文，不允许生成、不允许改写：\n"
            f"{chr(10).join(keep_lines)}\n\n"
            f"以下章节由用户手动编辑，不允许生成、不允许改写：\n"
            f"{chr(10).join(manual_lines)}\n\n"
            f"【JSON 字段结构预览】\n{json_schema_preview or '{}'}\n\n"
            f"【字段内容要求】\n"
            f"1. 字段内容必须与对应中文章节标题语义一致。\n"
            f"2. 字段内容要基于项目需求文档展开，不要编造需求文档中没有依据的设备、人员、审批信息或公司固定规范。\n"
            f"3. 如果某个允许生成字段缺少需求依据，也必须返回该字段，并用审慎、通用、可落地的测试方案文本描述。\n"
            f"4. 顶层字段只允许使用上方列出的字段名。\n"
            f"5. 普通文字章节建议返回 2-5 条高质量段落或要点，每条要能直接放入正式测试方案。\n"
            f"6. 表格章节必须返回对象数组，键名必须与模板表头完全一致，不要新增列、不要漏列。\n"
            f"7. 测试目标、测试范围、测试策略、风险分析等章节要包含可执行的测试动作、关注点、验收标准或应对措施，避免泛泛而谈。\n"
            f"8. 对需求文档没有明确说明的内容，只能使用通用但合理的测试工程表述，不能虚构具体数据。\n"
            f"9. 不要在任何字段值、段落、表格单元格中说明内容来源或处理方式，"
            '例如"AI生成""保留原文""手动编辑"。'
        )

    # ── Internal helpers ───────────────────────────────────────────

    def _load_rule_file(self, filename: str) -> str:
        """Load a rule file from the system_rules directory."""
        file_path = self.rules_dir / filename
        if not file_path.exists():
            logger.warning("规则文件不存在：%s，使用空规则", file_path)
            return ""
        try:
            return file_path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning("读取规则文件失败：%s | 错误：%s", file_path, exc)
            return ""

    @staticmethod
    def _format_ai_fields_summary(ai_fields: List[Dict[str, object]]) -> str:
        """Format field list in summary mode (field name + title only)."""
        lines = []
        for item in ai_fields:
            if isinstance(item, dict):
                field_name = item.get("field", "")
                title = item.get("title", "")
                lines.append(f"- {field_name}: {title}")
        return "\n".join(lines) if lines else "- 无"

    @staticmethod
    def _format_ai_fields_detail(ai_fields: List[Dict[str, object]]) -> str:
        """Format field list in detail mode (with table constraints and descriptions)."""
        lines = []
        for item in ai_fields:
            if isinstance(item, dict):
                field_name = item.get("field", "")
                title = item.get("title", "")
                section_id = item.get("section_id", "")
                level = item.get("level", "")
                description = str(item.get("description", "")).strip()
                table_schemas = item.get("table_schemas", [])

                table_meta = ""
                if isinstance(table_schemas, list) and table_schemas:
                    if len(table_schemas) == 1:
                        headers = (
                            table_schemas[0].get("headers", [])
                            if isinstance(table_schemas[0], dict)
                            else []
                        )
                        if headers:
                            complex_hint = (
                                "；该表格存在复杂格式，必须严格使用模板表头，不要新增列"
                                if table_schemas[0].get("complex")
                                else ""
                            )
                            table_meta = (
                                f"；该章节包含模板表格，字段内容必须返回对象数组，"
                                f"每个对象的键必须且只能使用这些表头："
                                f"{'、'.join(str(h) for h in headers)}{complex_hint}"
                            )
                    else:
                        table_descriptions = []
                        for idx, schema in enumerate(table_schemas):
                            if isinstance(schema, dict):
                                headers = schema.get("headers", [])
                                if headers:
                                    complex_hint = (
                                        "；该表格存在复杂格式，必须严格使用模板表头，不要新增列"
                                        if schema.get("complex")
                                        else ""
                                    )
                                    table_descriptions.append(
                                        f"表格{idx+1}表头：{'、'.join(str(h) for h in headers)}{complex_hint}"
                                    )
                        if table_descriptions:
                            table_meta = (
                                f"；该章节包含 {len(table_schemas)} 个模板表格，字段内容必须返回二维数组，"
                                f"外层数组的每个元素对应一个表格，内层数组是该表格的数据行。"
                                f"{'；'.join(table_descriptions)}"
                            )

                desc_hint = f"\n  模板章节说明：{description}" if description else ""
                meta = (
                    f"；模板位置 {section_id}；层级 {level}{table_meta}"
                    if section_id
                    else table_meta
                )
                lines.append(f"- {field_name}：{title}{desc_hint}{meta}")
        return "\n".join(lines) if lines else "- 无"


# ════════════════════════════════════════════════════════════════════════════════════════════════════
# 模块定位:Prompt builder(三 anchor 策略 ——
#   对抗 "Lost in the Middle" 现象,从 legacy prompt_builder.py 迁移)
#
#   Three-Anchor 策略:
#     1. 开头 anchor:摘要规则 + preset 格式预期
#     2. 中间 anchor:在需求文档后再次插入规则提醒
#     3. 末尾 anchor:完整详细规则,最终强化
#
#   已迁移功能:Two-Pass(outline + batch)生成方法在拆出前内部保留,
#   测试计划主路径只用单次 LLM 调用(F019 移除 Two-Pass 后)。
#
# 链路(任一 LLM 工具的 prompt 构造):
#   Tool / Service.build_prompt(...)
#     → PromptBuilder.build_three_anchor_prompt(system, rules, payload)
#     → system_prompt + rule reminder + payload + final_rules
#     → LLMClient.generate_with_system(...) → raw text
#
# 主要导出函数(从函数签名 `def build_*_prompt` 看):
#   - build_three_anchor_prompt(system, rules, payload)
#   - build_test_plan_generation_prompt(requirement, template, generation_config)
#   - build_chat_system_prompt(...) / build_intent_router_prompt(...)
#   - build_section_regen_prompt(section, violation)
#
# 关键约束:
#   - 中间 anchor 插入点要在用户内容之后,绝对不在末尾(防 lost in the middle);
#   - 任何 prompt 都要过 prompt_dump.maybe_dump()(开发环境);
#   - schema 与 system_prompt 分开(便于 unit test);
#   - 不向 prompt 注入内部路径 / API key(用 redactor in prompt_builder);
#   - 测试覆盖三 anchor 都出现(head + middle + tail 三处匹配)。
# ════════════════════════════════════════════════════════════════════════════════════════════════════
