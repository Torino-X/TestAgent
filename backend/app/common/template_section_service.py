"""
测试方案模板解析服务（从 legacy_sources 等价迁移）。

从 Word 模板中识别标题层级（样式/大纲/编号三种方式）、表格结构、
章节描述文字，构建树形章节结构，并自动推荐每个章节的生成模式。

与旧项目的差异（架构适配）：
- 文件路径来源改为 local_storage 的绝对路径
- 日志改为标准 logging
- 异常类型不变（ValueError/FileNotFoundError），Tool 层统一包装
- 移除了 load_mock_sections() 中的 Qt expanded 字段默认值设定
- 全部解析能力完整保留
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List

from docx import Document
from docx.oxml.ns import qn

from app.common.template_section import TemplateParseResult, TemplateSection

logger = logging.getLogger(__name__)


class TemplateSectionService:
    """模板解析与章节推荐服务。

    解析管线：读取 docx → 提取标题章节 → 分配正文范围 → 提取章节描述 →
    分配表格 schema → 构建树形结构 → 推荐生成模式。
    """

    # 默认设为"保留原文"的关键词
    KEEP_KEYWORDS = (
        "阶段测试策略", "测试轮次计划", "测试标准", "测试准入标准",
        "测试打回准则", "测试发布准则", "硬件环境", "修订记录",
    )
    # 标题文本超过此长度则不尝试编号识别
    MAX_HEADING_TEXT_LENGTH = 90
    # 已知的内容章节关键词，用于区分封面标题和正文标题
    CONTENT_KEYWORDS = (
        "项目概述", "测试目标", "测试范围", "测试策略", "测试进度",
        "准入准出", "风险分析", "测试交付物",
    )

    def parse_template(self, file_path: str) -> TemplateParseResult:
        """解析 Word 模板文件，返回包含树形章节结构和统计信息的解析结果。"""
        path = Path(file_path)
        if path.suffix.lower() != ".docx":
            raise ValueError("模板文件仅支持 .docx 格式")
        if not path.exists():
            raise FileNotFoundError("模板文件不存在")

        doc = Document(str(path))
        flat_sections = self._extract_heading_sections(doc)
        if not flat_sections:
            raise ValueError("未识别到模板标题层级，请确认 Word 模板使用了标题样式、大纲级别或常见编号标题。")
        sections = self._build_tree(flat_sections)
        self.apply_recommend_rule(sections)
        return TemplateParseResult(
            template_name=path.name,
            sections=sections,
            table_count=len(doc.tables),
            fixed_approval_count=self._count_fixed_approval_pages(flat_sections),
        )

    def load_mock_sections(self) -> List[TemplateSection]:
        """返回一组预设的模拟章节结构，用于无模板时的演示和测试。"""
        return [
            TemplateSection("1 项目概述", 1, children=[
                TemplateSection("1.1 项目背景", 2),
                TemplateSection("1.2 系统范围", 2),
            ]),
            TemplateSection("2 测试目标", 1),
            TemplateSection("3 测试范围", 1),
            TemplateSection("4 测试策略", 1),
            TemplateSection("5 测试环境", 1, "keep", False, children=[
                TemplateSection("5.1 测试设备清单", 2, "keep", False),
            ]),
            TemplateSection("6 测试资源", 1, "keep", False),
            TemplateSection("7 测试进度", 1),
            TemplateSection("8 准入准出标准", 1),
            TemplateSection("9 风险分析", 1),
            TemplateSection("10 测试交付物", 1),
            TemplateSection("11 审批信息", 1, "keep", False),
        ]

    def apply_recommend_rule(self, sections: List[TemplateSection]) -> None:
        """根据关键词自动推荐每个章节的生成模式（ai/keep/manual）。"""
        all_known = self.KEEP_KEYWORDS + self.CONTENT_KEYWORDS
        for section in self._walk(sections):
            # level=0 为封面/文档大标题，默认 AI 生成
            if section.level == 0:
                section.mode = "ai"
                section.checked = True
                continue
            should_keep = any(keyword in section.title for keyword in self.KEEP_KEYWORDS)
            # 第一个不匹配任何已知关键词的章节默认保留原文
            if not should_keep and not any(keyword in section.title for keyword in all_known):
                if sections and section is sections[0]:
                    should_keep = True
            section.mode = "keep" if should_keep else "ai"
            section.checked = not should_keep

    # ── 标题提取 ─────────────────────────────────────────────────

    def _extract_heading_sections(self, doc: Document) -> List[TemplateSection]:
        """遍历文档 body 子元素，识别标题段落并提取章节元信息。

        同时追踪表格位置，识别并跳过目录页，去重重复条目，
        最终为每个章节分配正文范围、表格 schema 和描述文字。
        """
        sections: List[TemplateSection] = []
        table_body_positions: List[tuple[int, int]] = []
        paragraph_iter = iter(doc.paragraphs)
        paragraph_index = 0
        table_index = 0
        last_body_index = 0
        toc_active = False

        for body_index, child in enumerate(doc.element.body.iterchildren()):
            last_body_index = body_index
            if child.tag == qn("w:tbl"):
                table_body_positions.append((body_index, table_index))
                table_index += 1
                continue
            if child.tag != qn("w:p"):
                continue

            try:
                para = next(paragraph_iter)
            except StopIteration:
                break

            text = para.text.strip()
            style_name = para.style.name if para.style else ""
            if not text:
                paragraph_index += 1
                continue

            if self._is_toc_title(text):
                toc_active = True
                paragraph_index += 1
                continue
            if toc_active:
                if self._is_toc_entry(para, style_name, text):
                    paragraph_index += 1
                    continue
                toc_active = False

            if self._is_toc_entry(para, style_name, text):
                paragraph_index += 1
                continue

            title_level, source = self._detect_heading_level(para, style_name, text)
            if title_level:
                sections.append(
                    TemplateSection(
                        title=text,
                        level=title_level,
                        paragraph_index=paragraph_index,
                        body_start_index=body_index,
                        source=source,
                    )
                )
            elif not title_level and self._is_title_style(style_name):
                sections.append(
                    TemplateSection(
                        title=text,
                        level=0,
                        paragraph_index=paragraph_index,
                        body_start_index=body_index,
                        source="title",
                    )
                )
            paragraph_index += 1

        sections = self._dedupe_repeated_toc_sections(sections)
        self._assign_section_ranges(sections, last_body_index, table_body_positions)
        self._extract_section_descriptions(sections, doc)
        self._assign_table_schemas(sections, doc)
        return sections

    def _detect_heading_level(self, para, style_name: str, text: str) -> tuple[int | None, str]:
        """按优先级尝试三种方式识别标题级别：样式 → 大纲级别 → 编号文本。"""
        level = self._heading_level_from_style(style_name)
        if level:
            return level, "style"

        if para is not None:
            level = self._heading_level_from_outline(para)
            if level:
                return level, "outline"

        level = self._heading_level_from_numbering(text)
        if level:
            return level, "numbering"

        return None, ""

    @staticmethod
    def _heading_level_from_style(style_name: str) -> int | None:
        """从 Word 段落样式名提取标题级别（如 'Heading 1' → 1，'标题 2' → 2）。"""
        normalized = re.sub(r"\s+", "", style_name.lower())
        for index in range(1, 10):
            if normalized in {f"heading{index}", f"标题{index}"}:
                return index
        return None

    @staticmethod
    def _heading_level_from_outline(para) -> int | None:
        """从段落 XML 的大纲级别属性（outlineLvl）提取标题级别（0-based → +1）。"""
        p_pr = para._p.pPr
        if p_pr is None or p_pr.outlineLvl is None:
            return None
        value = p_pr.outlineLvl.val
        try:
            return int(value) + 1
        except (TypeError, ValueError):
            return None

    def _heading_level_from_numbering(self, text: str) -> int | None:
        """从纯文本编号格式推断标题级别。

        支持格式：
        - 数字编号：'1. 标题'（级别=1）、'1.2.3 标题'（级别=3）
        - 中文序号：'第一章 xxx'（级别=1）、'一、xxx'（级别=1）
        - 括号序号：'（一）xxx'（级别=2）
        - 英文序号：'A. xxx'（级别=2）
        """
        compact = text.strip()
        if not compact or len(compact) > self.MAX_HEADING_TEXT_LENGTH:
            return None

        # Number-like prefixes are also common in ordinary requirement lists.
        # The previous fallback promoted every ``1. sentence`` paragraph to a
        # heading, which split the template body and turned prose placeholders
        # such as "请补充" into independently generated sections.  Only use
        # this text-only fallback for title-shaped text.  Proper Word heading
        # styles and outline levels remain authoritative and are handled above.
        if compact.endswith(("。", "；", "：", ";", ":", "！", "!", "？", "?")):
            return None

        numeric = re.match(r"^(\d+(?:\.\d+){0,8})(?:[\.、．\s\t]+)(\S.+)$", compact)
        if numeric:
            return min(numeric.group(1).count(".") + 1, 9)

        if re.match(r"^第[一二三四五六七八九十百千万\d]+[章节篇部分][\s：:、.-]*\S*", compact):
            return 1

        if re.match(r"^[一二三四五六七八九十]+[、．.]\s*\S+", compact):
            return 1

        if re.match(r"^[（(][一二三四五六七八九十]+[）)]\s*\S+", compact):
            return 2

        if re.match(r"^[A-Z][、．.]\s*\S+", compact):
            return 2

        return None

    # ── 目录页检测 ───────────────────────────────────────────────

    @staticmethod
    def _is_title_style(style_name: str) -> bool:
        """判断段落样式是否为文档封面/大标题样式（非章节标题）。

        识别 Word 内置 'Title' 样式以及中文环境中的'标题'样式（不带数字）。
        """
        if not style_name:
            return False
        normalized = style_name.strip().lower()
        if normalized == "title":
            return True
        normalized_no_space = re.sub(r"\s+", "", normalized)
        if normalized_no_space == "标题":
            return True
        return False

    @staticmethod
    def _is_toc_title(text: str) -> bool:
        """判断文本是否为目录标题（"目录"、"目次"、"Table of Contents"等）。"""
        compact = re.sub(r"\s+", "", text)
        return compact in {"目录", "目次", "contents", "tableofcontents"}

    @staticmethod
    def _is_toc_entry(para, style_name: str, text: str) -> bool:
        """判断段落是否为目录条目（TOC 样式、包含制表符或页码引导点）。"""
        normalized_style = re.sub(r"\s+", "", style_name.lower())
        if normalized_style.startswith("toc") or normalized_style.startswith("目录"):
            return True
        if "\t" in para.text:
            return True
        if re.search(r"\.{2,}\s*\d+$", text):
            return True
        return False

    @staticmethod
    def _dedupe_repeated_toc_sections(sections: List[TemplateSection]) -> List[TemplateSection]:
        """去除因目录页和正文重复出现的同一章节条目，保留正文中首次出现的那一个。"""
        if not sections:
            return sections

        first_index_by_key: dict[tuple[int, str], int] = {}
        duplicate_keys: set[tuple[int, str]] = set()
        for index, section in enumerate(sections):
            key = (section.level, TemplateSectionService._normalize_title_key(section.title))
            if key in first_index_by_key:
                duplicate_keys.add(key)
            else:
                first_index_by_key[key] = index

        if not duplicate_keys:
            return sections

        return [
            section
            for index, section in enumerate(sections)
            if (section.level, TemplateSectionService._normalize_title_key(section.title)) not in duplicate_keys
            or index != first_index_by_key[(section.level, TemplateSectionService._normalize_title_key(section.title))]
        ]

    @staticmethod
    def _normalize_title_key(title: str) -> str:
        """去除标题中的空白和目录页码（如 '...​12'），用于去重比较。"""
        compact = re.sub(r"\s+", "", title)
        compact = re.sub(r"\.{2,}\d+$", "", compact)
        return compact.strip()

    # ── 章节范围与表格分配 ───────────────────────────────────────

    def _assign_section_ranges(
        self,
        flat_sections: List[TemplateSection],
        last_body_index: int,
        table_body_positions: List[tuple[int, int]],
    ) -> None:
        """为每个章节计算正文结束位置（body_end_index）和归属的表格索引列表。

        结束位置 = 下一个同级或上级标题的 body_start_index - 1，无下一同级标题则为文档末尾。
        """
        for index, section in enumerate(flat_sections):
            if section.body_start_index is None:
                continue
            end_index = last_body_index
            for next_section in flat_sections[index + 1:]:
                if next_section.body_start_index is not None and next_section.level <= section.level:
                    end_index = max(section.body_start_index, next_section.body_start_index - 1)
                    break
            section.body_end_index = end_index
            section.table_indexes = [
                table_index
                for body_index, table_index in table_body_positions
                if section.body_start_index <= body_index <= end_index
            ]

    def _assign_table_schemas(self, flat_sections: List[TemplateSection], doc: Document) -> None:
        """为每个章节构建归属表格的 schema 元信息（表头、行列数、是否复杂表格），跳过封面布局表格。"""
        for section in flat_sections:
            schemas = []
            for table_index in section.table_indexes:
                if table_index < 0 or table_index >= len(doc.tables):
                    continue
                table = doc.tables[table_index]
                if self._is_cover_layout_table(table, section):
                    continue
                headers = self._table_headers(table)
                schemas.append({
                    "table_index": table_index,
                    "headers": headers,
                    "row_count": len(table.rows),
                    "col_count": len(table.columns),
                    "complex": self._is_complex_table(table),
                })
            section.table_schemas = schemas

    @staticmethod
    def _is_cover_layout_table(table, section: TemplateSection) -> bool:
        """检测表格是否为封面/布局表格（非数据表）。

        判断依据：表格第一行任意单元格的文本包含章节标题，
        说明该表格用于展示标题信息而非承载数据列。
        """
        section_title = section.title.strip()
        if not section_title or not table.rows:
            return False
        for cell in table.rows[0].cells:
            if section_title in cell.text:
                return True
        return False

    @staticmethod
    def _table_headers(table) -> List[str]:
        """提取表格第一行单元格文本作为列头，重复列名自动追加序号去重。"""
        if not table.rows:
            return []
        headers = []
        used: set[str] = set()
        for index, cell in enumerate(table.rows[0].cells, start=1):
            text = cell.text.strip() or f"列{index}"
            if text in used:
                text = f"{text}_{index}"
            used.add(text)
            headers.append(text)
        return headers

    @staticmethod
    def _is_complex_table(table) -> bool:
        """检测表格是否包含合并单元格、嵌套表格或嵌入对象，标记为复杂表格。"""
        element = table._tbl
        complex_tags = {
            qn("w:gridSpan"),
            qn("w:vMerge"),
            qn("w:drawing"),
            qn("w:pict"),
            qn("w:object"),
            qn("w:sdt"),
        }
        if any(node.tag in complex_tags for node in element.iter()):
            return True
        for cell in element.iter(qn("w:tc")):
            if cell.findall(qn("w:tbl")):
                return True
        return False

    # ── 描述文字提取 ─────────────────────────────────────────────

    def _extract_section_descriptions(self, flat_sections: List[TemplateSection], doc: Document) -> None:
        """提取每个章节标题下方的描述性文字（标题到第一个子标题之间的非标题段落）。

        描述文字通常是模板中指导该章节应填写什么内容的说明，提取后存入
        section.description，供下游 PromptBuilder 作为生成参考传递。
        """
        heading_positions = {s.body_start_index for s in flat_sections if s.body_start_index is not None}

        child_start_map: dict[int, int | None] = {}
        for section in flat_sections:
            if section.body_start_index is None or section.body_end_index is None:
                continue
            first_child_start = None
            for other in flat_sections:
                if other is section or other.body_start_index is None:
                    continue
                if other.level <= section.level:
                    continue
                if section.body_start_index < other.body_start_index <= section.body_end_index:
                    if first_child_start is None or other.body_start_index < first_child_start:
                        first_child_start = other.body_start_index
            child_start_map[section.body_start_index] = first_child_start

        for section in flat_sections:
            if section.body_start_index is None:
                continue
            first_child_start = child_start_map.get(section.body_start_index)
            desc_stop = (
                first_child_start
                if first_child_start is not None
                else (section.body_end_index or section.body_start_index)
            )

            desc_parts: List[str] = []
            for body_index, child in enumerate(doc.element.body.iterchildren()):
                if body_index <= section.body_start_index:
                    continue
                if body_index >= desc_stop:
                    break
                if body_index in heading_positions:
                    continue
                if child.tag == qn("w:p"):
                    if not self._is_italic_paragraph(child):
                        continue
                    text = self._element_text(child).strip()
                    if text:
                        desc_parts.append(text)

            if desc_parts:
                section.description = "\n".join(desc_parts)

    @staticmethod
    def _element_text(element) -> str:
        """提取 docx XML 元素中的所有文本（遍历 w:t 节点）。"""
        return "".join(node.text or "" for node in element.iter(qn("w:t")))

    @staticmethod
    def _is_italic_paragraph(para_element) -> bool:
        """若段落任一 run 带有斜体标记（w:i 或 w:iCs），返回 True。"""
        if para_element.tag != qn("w:p"):
            return False
        for run in para_element.findall(qn("w:r")):
            rpr = run.find(qn("w:rPr"))
            if rpr is None:
                continue
            for tag in (qn("w:i"), qn("w:iCs")):
                i_elem = rpr.find(tag)
                if i_elem is not None:
                    val = i_elem.get(qn("w:val"))
                    if val is None or val in ("true", "1"):
                        return True
        return False

    # ── 树构建 ───────────────────────────────────────────────────

    @staticmethod
    def _build_tree(flat_sections: List[TemplateSection]) -> List[TemplateSection]:
        """使用栈将扁平章节列表转换为树形结构：级别更深的成为前一个章节的子节点。"""
        roots: List[TemplateSection] = []
        stack: List[TemplateSection] = []
        for section in flat_sections:
            while stack and stack[-1].level >= section.level:
                stack.pop()
            if stack:
                stack[-1].children.append(section)
            else:
                roots.append(section)
            stack.append(section)
        return roots

    @staticmethod
    def _count_fixed_approval_pages(sections: List[TemplateSection]) -> int:
        """统计包含审批/签署/修订关键词的章节数量，用于模板解析结果概览。"""
        keywords = ("审批", "签署", "修订", "批准")
        return sum(1 for section in sections if any(keyword in section.title for keyword in keywords))

    @staticmethod
    def _walk(sections: List[TemplateSection]) -> List[TemplateSection]:
        """递归将树形章节结构展平为一维列表，便于遍历所有节点。"""
        result: List[TemplateSection] = []
        for section in sections:
            result.extend(section.walk())
        return result


# ── F025 — review_standard synthesis ────────────────────────────────
#
# Builds a ``review_standard`` spec from the parsed template + the
# generation_config produced by ``build_generation_config``.  The spec
# is consumed by ``ResultReviewTool`` (rule engine) and by the
# orchestrator's review-regen loop.
#
# Why a module-level function (not a class method):
#  * Pure: no DB / no I/O, easy to unit-test
#  * ``TemplateParserTool`` calls it after ``TemplateSectionService`` has
#    finished parsing; keeping it at module level avoids a circular
#    dependency with the parser class.

# Forbidden patterns injected into every template.  These represent
# hard project-wide rules the LLM has historically violated: markdown
# fences, bold markers, headings, inline code, HTML tags, JSON fences.
_DEFAULT_FORBIDDEN_PATTERNS = [
    "```",        # code fence
    "\\*\\*",     # bold marker
    "^#+\\s",     # heading marker
    "`[^`]+`",    # inline code
    "<[a-zA-Z]",  # HTML tag open
    "‘‘",         # smart-quote-only block marker (we keep 中英文 punctuation)
]

# Max characters per section body before we issue a "warn" finding.
# Templates routinely cap a section at one A4 page (~2000–3000 chars);
# 4000 gives headroom for tables + descriptions.
_DEFAULT_MAX_CHARS = 4000


def build_review_standard(
    *,
    template_id: str | None,
    sections: list[TemplateSection] | None,
    generation_config: dict | None,
) -> dict:
    """Build a F025 review_standard spec from a parsed template.

    Args:
        template_id: informational id (the template's public_id, may be
            ``None`` if not yet persisted).
        sections: the parsed section tree (used to detect "summary"
            sections and to extract per-section metadata).
        generation_config: the ``generation_config`` dict emitted by
            ``build_generation_config`` — its ``ai_fields[*]`` carry
            per-section ``table_schemas`` with explicit ``headers``
            that we convert into ``table_header_whitelist`` rules.

    Returns:
        A ``review_standard`` v1 dict (see plan for schema).  Always
        populated with at least the two default rules
        (``no_markdown_body`` block + ``section_too_long`` warn).
    """
    rules: list[dict] = []

    # ── 1. Default forbidden-pattern rule (block) ────────────────
    rules.append({
        "id": "no_markdown_body",
        "kind": "forbidden_pattern",
        "scope": "section.body",
        "patterns": list(_DEFAULT_FORBIDDEN_PATTERNS),
        "severity": "block",
        "applies_to_sections": ["*"],
    })

    # ── 2. Per-table header whitelist (block) ───────────────────
    #    Synthesised from generation_config.ai_fields[*].table_schemas
    #    (the same source that the LLM is told to obey).
    if isinstance(generation_config, dict):
        ai_fields = generation_config.get("ai_fields", [])
        if isinstance(ai_fields, list):
            for item in ai_fields:
                if not isinstance(item, dict):
                    continue
                field_name = str(item.get("field", "")).strip()
                title = str(item.get("title", "")).strip()
                if not field_name:
                    continue
                schemas = item.get("table_schemas", [])
                if not isinstance(schemas, list):
                    continue
                for schema in schemas:
                    if not isinstance(schema, dict):
                        continue
                    headers = schema.get("headers", [])
                    if not isinstance(headers, list) or not headers:
                        continue
                    whitelist = [str(h) for h in headers]
                    rules.append({
                        "id": f"table_header_{field_name}",
                        "kind": "table_header_whitelist",
                        "scope": "section.tables[*].header",
                        "section_id": field_name,
                        "section_title": title,
                        "whitelist": whitelist,
                        "severity": "block",
                        # Phase 2.9A.X：fix_instruction 由 ResultReviewTool
                        # 透传给 TestPlanRegenTool._build_prompt，注入到
                        # LLM 修正 prompt 里（替代 message 自然语言）。
                        "fix_instruction": (
                            f"将该字段（含违反表头的行）每行 row 的 keys 改为"
                            f" whitelist 中的键名。"
                            f"允许的键名集合（必须且只能使用这些）："
                            + "、".join(f"「{h}」" for h in whitelist)
                            + "。禁止新增、删除、改名或简写。"
                        ),
                    })

    # ── 2.5. missing_section rule (block) ─────────────────────────
    #    Phase 2.9A.X：宽容解析 / 整体 JSON 截断场景下，结果产物可能缺
    #    少某些章节。ResultReviewTool 在 review 时与 template_structure
    #    比对，发现缺失章节 emit block issue，RepairAgent 调
    #    TestPlanRegenTool 定点补生成。
    if isinstance(sections, list) and sections:
        expected_section_ids = []
        for sec in sections:
            if sec is None:
                continue
            sid = getattr(sec, "id", None) or (
                sec.get("id") if isinstance(sec, dict) else None
            )
            if sid and sid not in expected_section_ids:
                expected_section_ids.append(str(sid))
        if expected_section_ids:
            rules.append({
                "id": "missing_section",
                "kind": "missing_section",
                "scope": "test_plan_content.section_package",
                "expected_section_ids": expected_section_ids,
                "severity": "block",
                "fix_instruction": (
                    "以下章节在生成结果中未出现："
                    + "、".join(f"「{sid}」" for sid in expected_section_ids)
                    + "。请基于模板对应章节内容，使用 ``TestPlanRegenTool`` "
                    "为这些章节单独生成内容（不要重写其他章节）。"
                ),
            })

    # ── 2.6. template_backfill rule (block) ───────────────────────
    #    BUG FIX 2026-08-18：模板声明某章节不应有 tables 字段，但 LLM
    #    仍可能生成 tables（用户场景：strategy_2 模板 mode="ai" +
    #    table_indexes=[] + LLM 返回 tables=[...]）。该问题原本延迟到
    #    WordExportTool 导出后才由 DocxFormatCheckTool 检测到 drift/loss，
    #    移到这里提前拦截，让 RepairAgent 调 TestPlanRegenTool 定点修复。
    #
    #    触发条件精确（避免误报）：
    #      - mode == "ai"  （keep/manual 不需要此规则，后续可扩展）
    #      - table_indexes 为空（模板本身无表格占位）
    #      - leaf section   （非 leaf 的子章节各自有 table_indexes）
    #    不触发的章节：mode="keep"/"manual" 或 table_indexes 非空或非 leaf。
    if isinstance(sections, list) and sections:
        for order, sec in enumerate(sections):
            if sec is None or not isinstance(sec, TemplateSection):
                continue
            if getattr(sec, "mode", "ai") != "ai":
                continue
            table_indexes = getattr(sec, "table_indexes", None) or []
            if not isinstance(table_indexes, list) or len(table_indexes) > 0:
                continue
            children = getattr(sec, "children", None) or []
            if children:  # 非 leaf section 跳过（子章节各自校验）
                continue
            # 合成稳定 section_id，与 ai_fields[*].section_id 同源
            # (template_prompt_preview_service._section_id)。
            body_start = getattr(sec, "body_start_index", None)
            paragraph_idx = getattr(sec, "paragraph_index", None)
            level = getattr(sec, "level", 1)
            if body_start is not None:
                sid = f"body_{body_start}_level_{level}"
            elif paragraph_idx is not None:
                sid = f"para_{paragraph_idx}_level_{level}"
            else:
                sid = f"section_{order}_level_{level}"
            title = getattr(sec, "title", "") or sid
            rules.append({
                "id": f"no_tables_for_{sid}",
                "kind": "template_backfill",
                "scope": "section.tables",
                "section_id": sid,
                "section_title": title,
                "expected_no_tables": True,
                "severity": "block",
                "applies_to_sections": [sid],
                "fix_instruction": (
                    f"章节「{title}」在模板中未声明任何表格占位，"
                    f"请重新生成该章节，仅返回 content 字段"
                    f"（纯文本/列表），不要返回 tables 数组。"
                ),
            })

    # ── 3. Summary-as-bullet-list rule (block) ──────────────────
    #    Triggered when the template has a section whose title contains
    #    "概述" / "总结" / "summary" — the legacy pipeline has
    #    historically generated them as plain prose.
    if sections is not None:
        for sec in sections:
            title = getattr(sec, "title", "") or ""
            if not title:
                continue
            t_low = title.lower()
            if any(kw in title for kw in ("概述", "总结")) or "summary" in t_low:
                rules.append({
                    "id": f"summary_bullet_{getattr(sec, 'id', title)}",
                    "kind": "section_format",
                    "scope": "section",
                    "section_id": getattr(sec, "id", title),
                    "section_title": title,
                    "format": "bullet_list",
                    "min_items": 3,
                    "severity": "block",
                })
                break  # one summary rule is enough

    # ── 4. Default max-chars rule (warn) ────────────────────────
    rules.append({
        "id": "section_too_long",
        "kind": "max_chars",
        "scope": "section",
        "max_chars": _DEFAULT_MAX_CHARS,
        "severity": "warn",
    })

    return {
        "version": 1,
        "template_id": template_id or "",
        "rules": rules,
        "pass_policy": "any_block_fails",
    }


# 模块定位:测试方案模板解析服务(756 行核心支撑)
#
# 从 Word 模板识别:
#   - 标题层级(样式 / 大纲 / 编号三种方式);
#   - 表格结构(headers / table_schemas);
#   - 章节描述文字(斜体注释);
#   - 树形章节结构;
#   - 每个章节推荐生成模式(ai / keep / manual)。
#
# 链路:
#   TemplateParserTool.run() → TemplateSectionService.parse(path)
#     → 写 state.template_structure + state.review_standard
#
# 关键约束:
#   - 阅读"Word 模板"是真的 docx,**不要**靠文本启发式解析标题;
#   - 章节标题层级是测试方案章节结构的源头,改它要全面回归;
#   - 与 template_prompt_preview_service 协同(后者消费前者输出);
#   - 是 Phase 2.9A.20 review_standard 合成的唯一入口。
