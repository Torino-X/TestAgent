"""Tests for adapted common modules (batch 1 migration).

Tests DocumentReader, TemplateSectionService, TemplatePromptPreviewService,
and the two rewritten tools.
"""

import os
import tempfile
from pathlib import Path

import pytest

from app.common.document_reader import DocumentReader, DocumentReadError, DocumentReadResult, RequirementBlock
from app.common.template_section import TemplateSection, TemplateParseResult
from app.common.template_section_service import TemplateSectionService
from app.common.template_prompt_preview_service import TemplatePromptPreviewService


# ── DocumentReader tests ─────────────────────────────────────────


class TestDocumentReaderTxtMd:
    """Test .txt and .md file reading."""

    def test_read_md_utf8(self):
        reader = DocumentReader()
        content = "# Title\n\n## Section 1\n\nParagraph text here."
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", encoding="utf-8", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = reader.read_structured(path)
            assert len(result.blocks) == 1
            assert result.blocks[0].block_type == "text"
            assert "Title" in result.blocks[0].text
        finally:
            os.unlink(path)

    def test_read_txt_utf8(self):
        reader = DocumentReader()
        content = "Line 1\nLine 2\nLine 3"
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", encoding="utf-8", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = reader.read_structured(path)
            assert len(result.blocks) == 1
            assert result.blocks[0].block_type == "text"
            assert result.blocks[0].text == content
        finally:
            os.unlink(path)

    def test_read_txt_gbk_fallback(self):
        reader = DocumentReader()
        content = "中文内容测试"
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="wb", delete=False) as f:
            f.write(content.encode("gbk"))
            path = f.name
        try:
            result = reader.read_structured(path)
            assert len(result.blocks) == 1
            assert result.blocks[0].block_type == "text"
            assert "中文" in result.blocks[0].text
        finally:
            os.unlink(path)

    def test_unsupported_extension(self):
        reader = DocumentReader()
        with pytest.raises(DocumentReadError, match="不支持的"):
            reader.read_structured("/nonexistent/file.pdf")

    def test_file_not_found(self):
        reader = DocumentReader()
        with pytest.raises(DocumentReadError, match="不存在"):
            reader.read_structured("/nonexistent/file.docx")

    def test_to_prompt_text_simple(self):
        blocks = [
            RequirementBlock("text", text="Paragraph 1"),
            RequirementBlock("table", text="col1 | col2", index=1),
        ]
        result = DocumentReadResult(blocks)
        text = result.to_prompt_text()
        assert "Paragraph 1" in text
        assert "表格 1" in text
        assert "col1 | col2" in text

    def test_image_blocks_filter(self):
        blocks = [
            RequirementBlock("text", text="Some text"),
            RequirementBlock("image", index=1, image_path=Path("/tmp/img1.png")),
            RequirementBlock("image", index=2, image_path=Path("/tmp/img2.png")),
        ]
        result = DocumentReadResult(blocks)
        assert len(result.image_blocks) == 2


class TestDocumentReaderDocx:
    """Test .docx file reading with a programmatically created file."""

    @pytest.fixture
    def sample_docx_path(self):
        """Create a minimal .docx with headings, paragraphs, and a table."""
        from docx import Document
        doc = Document()
        doc.add_heading("需求文档标题", level=0)
        doc.add_heading("第一章 概述", level=1)
        doc.add_paragraph("这是一段普通的正文段落。")
        doc.add_paragraph("这是另一段正文，包含更加详细的需求描述。")
        doc.add_heading("第二章 功能需求", level=1)
        doc.add_heading("2.1 用户管理", level=2)
        doc.add_paragraph("用户管理模块的描述文字。")
        # Add a table
        table = doc.add_table(rows=3, cols=3, style="Table Grid")
        table.cell(0, 0).text = "功能项"
        table.cell(0, 1).text = "优先级"
        table.cell(0, 2).text = "说明"
        table.cell(1, 0).text = "用户登录"
        table.cell(1, 1).text = "高"
        table.cell(1, 2).text = "通过账号密码登录"
        table.cell(2, 0).text = "用户注销"
        table.cell(2, 1).text = "中"
        table.cell(2, 2).text = "安全退出会话"

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            doc.save(f.name)
            yield f.name
        os.unlink(f.name)

    def test_read_docx_structure(self, sample_docx_path):
        reader = DocumentReader()
        result = reader.read_structured(sample_docx_path)
        assert len(result.blocks) > 0
        # Should have text blocks (headings + paragraphs)
        text_blocks = [b for b in result.blocks if b.block_type == "text"]
        assert len(text_blocks) > 0
        # Should have at least one table block
        table_blocks = [b for b in result.blocks if b.block_type == "table"]
        assert len(table_blocks) >= 1
        # Table text should have pipe separators
        table_text = table_blocks[0].text
        assert "|" in table_text


# ── TemplateSection tests ────────────────────────────────────────


class TestTemplateSection:
    def test_walk_flat(self):
        s1 = TemplateSection("1 概述", 1)
        s2 = TemplateSection("2 范围", 1)
        assert len(s1.walk()) == 1
        all_sections = s1.walk() + s2.walk()
        assert len(all_sections) == 2

    def test_walk_tree(self):
        parent = TemplateSection("1 概述", 1, children=[
            TemplateSection("1.1 背景", 2),
            TemplateSection("1.2 目标", 2, children=[
                TemplateSection("1.2.1 质量目标", 3),
            ]),
        ])
        all_sections = parent.walk()
        # parent + 2 children + 1 grandchild = 4
        assert len(all_sections) == 4

    def test_to_dict(self, simple_section):
        d = simple_section.to_dict()
        assert d["title"] == "1 项目概述"
        assert d["level"] == 1
        assert d["mode"] == "ai"
        assert d["children"] == []

    def test_to_dict_nested(self):
        parent = TemplateSection("1 概述", 1, children=[
            TemplateSection("1.1 背景", 2),
        ])
        d = parent.to_dict()
        assert len(d["children"]) == 1
        assert d["children"][0]["title"] == "1.1 背景"


class TestTemplateParseResult:
    def test_all_sections(self, parse_result_with_sections):
        all_s = parse_result_with_sections.all_sections()
        assert len(all_s) == 3

    def test_top_level_count(self, parse_result_with_sections):
        assert parse_result_with_sections.top_level_count == 2

    def test_child_count(self, parse_result_with_sections):
        assert parse_result_with_sections.child_count == 1

    def test_to_dict(self, parse_result_with_sections):
        d = parse_result_with_sections.to_dict()
        assert d["template_name"] == "测试模板.docx"
        assert d["table_count"] == 5
        assert len(d["sections"]) == 2


# ── TemplateSectionService tests ─────────────────────────────────


class TestHeadingDetection:
    def test_style_detection_english(self):
        level = TemplateSectionService._heading_level_from_style("Heading 2")
        assert level == 2

    def test_style_detection_chinese(self):
        level = TemplateSectionService._heading_level_from_style("标题 3")
        assert level == 3

    def test_style_not_heading(self):
        level = TemplateSectionService._heading_level_from_style("Normal")
        assert level is None

    def test_numbering_numeric_single(self):
        service = TemplateSectionService()
        level, source = service._detect_heading_level(None, "Normal", "1. 项目概述")
        assert level == 1
        assert source == "numbering"

    def test_numbering_numeric_nested(self):
        service = TemplateSectionService()
        level, source = service._detect_heading_level(None, "Normal", "1.2.3 详细设计")
        assert level == 3
        assert source == "numbering"

    def test_numbering_chinese_chapter(self):
        service = TemplateSectionService()
        level, source = service._detect_heading_level(None, "Normal", "第一章 项目背景")
        assert level == 1
        assert source == "numbering"

    def test_numbering_chinese_dash(self):
        service = TemplateSectionService()
        level, source = service._detect_heading_level(None, "Normal", "一、概述")
        assert level == 1
        assert source == "numbering"

    def test_numbering_parenthesis(self):
        service = TemplateSectionService()
        level, source = service._detect_heading_level(None, "Normal", "（一）详细说明")
        assert level == 2
        assert source == "numbering"

    def test_numbering_english(self):
        service = TemplateSectionService()
        level, source = service._detect_heading_level(None, "Normal", "A. Introduction")
        assert level == 2
        assert source == "numbering"

    def test_not_heading_too_long(self):
        service = TemplateSectionService()
        long_text = "这是一段非常长的文本" * 10
        level, _ = service._detect_heading_level(None, "Normal", long_text)
        assert level is None

    def test_not_heading_plain_text(self):
        service = TemplateSectionService()
        level, _ = service._detect_heading_level(None, "Normal", "这是一段普通的描述性文字没有编号前缀")
        assert level is None


class TestTocDetection:
    def test_is_toc_title(self):
        assert TemplateSectionService._is_toc_title("目录") is True
        assert TemplateSectionService._is_toc_title("目 录") is True
        assert TemplateSectionService._is_toc_title("第一章") is False

    def test_is_toc_entry_dots(self):
        # Create a minimal mock paragraph
        class MockPara:
            text = "项目概述...........3"
        assert TemplateSectionService._is_toc_entry(MockPara(), "Normal", "项目概述...........3") is True


class TestDedup:
    def test_dedupe_removes_duplicates(self):
        """TOC entries (lower index) are discarded, body entries (higher index) kept."""
        s1 = TemplateSection("1 概述", 1, paragraph_index=1, body_start_index=1)
        s2 = TemplateSection("1 概述", 1, paragraph_index=5, body_start_index=5)
        result = TemplateSectionService._dedupe_repeated_toc_sections([s1, s2])
        assert len(result) == 1
        # Keeps the later occurrence (body text, not TOC duplicate)
        assert result[0].paragraph_index == 5

    def test_dedupe_no_duplicates(self):
        s1 = TemplateSection("1 概述", 1, paragraph_index=1)
        s2 = TemplateSection("2 范围", 1, paragraph_index=2)
        result = TemplateSectionService._dedupe_repeated_toc_sections([s1, s2])
        assert len(result) == 2


class TestBuildTree:
    def test_build_flat(self):
        s1 = TemplateSection("1 概述", 1)
        s2 = TemplateSection("2 范围", 1)
        tree = TemplateSectionService._build_tree([s1, s2])
        assert len(tree) == 2
        assert tree[0].children == []
        assert tree[1].children == []

    def test_build_nested(self):
        s1 = TemplateSection("1 概述", 1)
        s2 = TemplateSection("1.1 背景", 2)
        s3 = TemplateSection("1.2 目标", 2)
        s4 = TemplateSection("2 范围", 1)
        tree = TemplateSectionService._build_tree([s1, s2, s3, s4])
        assert len(tree) == 2
        assert len(tree[0].children) == 2
        assert tree[0].children[0].title == "1.1 背景"
        assert tree[0].children[1].title == "1.2 目标"

    def test_build_deep_nesting(self):
        s1 = TemplateSection("1 概述", 1)
        s2 = TemplateSection("1.1 背景", 2)
        s3 = TemplateSection("1.1.1 历史", 3)
        tree = TemplateSectionService._build_tree([s1, s2, s3])
        assert len(tree) == 1
        assert len(tree[0].children) == 1
        assert len(tree[0].children[0].children) == 1


class TestRecommendRule:
    def test_level_zero_always_ai(self):
        section = TemplateSection("文档标题", 0)
        service = TemplateSectionService()
        service.apply_recommend_rule([section])
        assert section.mode == "ai"

    def test_keep_keyword_sections(self):
        service = TemplateSectionService()
        section = TemplateSection("硬件环境要求", 1)
        service.apply_recommend_rule([section])
        assert section.mode == "keep"

    def test_unknown_section_first_level_keep(self):
        service = TemplateSectionService()
        section = TemplateSection("其他说明", 1)
        service.apply_recommend_rule([section])
        assert section.mode == "keep"

    def test_default_ai_for_known_content(self):
        service = TemplateSectionService()
        section = TemplateSection("项目概述说明", 1)
        service.apply_recommend_rule([section])
        assert section.mode == "ai"


class TestTableSchemas:
    def test_complex_table_detection_gridspan(self):
        """A table with gridSpan is detected as complex."""
        from docx import Document
        from docx.oxml.ns import qn
        from lxml import etree
        doc = Document()
        table = doc.add_table(rows=2, cols=2)
        # Add gridSpan to make it complex
        tc_pr = table.cell(0, 0)._tc.get_or_add_tcPr()
        # Use Clark notation for the attribute name to avoid qn() limitations
        grid_span = etree.SubElement(tc_pr, qn("w:gridSpan"))
        grid_span.set(qn("w:val"), "2")
        assert TemplateSectionService._is_complex_table(table) is True

    def test_simple_table_not_complex(self):
        from docx import Document
        doc = Document()
        table = doc.add_table(rows=2, cols=2, style="Table Grid")
        assert TemplateSectionService._is_complex_table(table) is False

    def test_table_headers(self):
        from docx import Document
        doc = Document()
        table = doc.add_table(rows=1, cols=3)
        table.cell(0, 0).text = "Name"
        table.cell(0, 1).text = "Type"
        table.cell(0, 2).text = "Desc"
        headers = TemplateSectionService._table_headers(table)
        assert headers == ["Name", "Type", "Desc"]

    def test_table_headers_duplicate_names(self):
        from docx import Document
        doc = Document()
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "备注"
        table.cell(0, 1).text = "备注"
        headers = TemplateSectionService._table_headers(table)
        assert headers[0] == "备注"
        assert headers[1] == "备注_2"


# ── TemplatePromptPreviewService tests ────────────────────────────


class TestFieldMapping:
    def test_section_to_field_known(self):
        service = TemplatePromptPreviewService()
        assert service.section_to_field("项目概述") == "overview"
        assert service.section_to_field("测试策略说明") == "strategy"
        assert service.section_to_field("风险分析") == "risks"

    def test_section_to_field_numeric_fallback(self):
        service = TemplatePromptPreviewService()
        field = service.section_to_field("3.2.1 详细方案设计")
        assert field == "section_3_2_1"

    def test_section_to_field_ascii_fallback(self):
        service = TemplatePromptPreviewService()
        field = service.section_to_field("System Architecture Design")
        assert "system" in field.lower()

    def test_section_to_field_index_fallback(self):
        """既有数字也无 ASCII 词的中文标题 — 显式无 section_id 时回退到
        ``field_<fallback_index>``（**禁止**用裸 ``section_<idx>``，否则会
        与章节顺序耦合，且与数字章节 ``section_2`` 命名冲突）。
        """
        service = TemplatePromptPreviewService()
        field = service.section_to_field("其他杂项", 5)
        assert field == "field_5"

    def test_section_to_field_chinese_uses_section_id(self):
        """P0 修复：纯中文标题（既无数字也无 ASCII 词）必须用 section_id
        派生稳定 field 名,不再依赖 binding 顺序。该测试模拟"审批信息"
        章节触发 fallback 第三分支的场景。
        """
        service = TemplatePromptPreviewService()
        # 传 section_id 时应基于 section_id 派生
        with_id = service.section_to_field(
            "审批信息", 2, section_id="body_14_level_1",
        )
        assert with_id == "field_body_14_level_1"
        # 不传 section_id 时退化为 field_<index>（已是上一条断言的语义）
        assert service.section_to_field("审批信息", 2) == "field_2"

    def test_section_to_field_stable_across_order(self):
        """P0 修复的根因保护：同一标题不管 binding 顺序如何，field 名必须
        仅取决于 section_id 而不是 fallback_index。"""
        service = TemplatePromptPreviewService()
        # 同一 "审批信息" 章节, order 不同（fallback_index 不同）
        # 但 section_id 相同 → 必须得到相同 field
        f_a = service.section_to_field(
            "审批信息", 2, section_id="body_14_level_1",
        )
        f_b = service.section_to_field(
            "审批信息", 7, section_id="body_14_level_1",
        )
        assert f_a == f_b == "field_body_14_level_1"

    def test_dedupe_field(self):
        service = TemplatePromptPreviewService()
        used = {"overview"}
        assert service._dedupe_field("overview", used) == "overview_2"


class TestSectionBindings:
    def test_bindings_flat(self, flat_template_sections):
        service = TemplatePromptPreviewService()
        bindings = service.section_bindings(flat_template_sections)
        assert len(bindings) == 3
        assert bindings[0]["title"] == "项目概述"
        assert bindings[0]["field"] == "overview"
        assert bindings[1]["title"] == "测试策略"
        assert bindings[1]["field"] == "strategy"

    def test_bindings_nested(self, nested_template_sections):
        service = TemplatePromptPreviewService()
        bindings = service.section_bindings(nested_template_sections)
        assert len(bindings) == 3  # parent + 2 children



class TestGenerationConfig:
    def test_build_generation_config(self, mixed_mode_sections):
        service = TemplatePromptPreviewService()
        config = service.build_generation_config(mixed_mode_sections)
        assert config["template_map_version"] == 1
        assert len(config["ai_fields"]) >= 1  # at least one ai section
        assert len(config["section_bindings"]) == 3
        assert "json_schema_preview" in config


class TestJsonPreview:
    def test_build_json_preview_empty(self):
        service = TemplatePromptPreviewService()
        preview = service.build_json_preview([])
        assert preview == "{}"

    def test_build_json_preview_with_sections(self, simple_section):
        service = TemplatePromptPreviewService()
        sections = [simple_section]  # mode="ai" by default
        preview = service.build_json_preview(sections)
        assert "overview" in preview or "section_1" in preview


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def simple_section():
    return TemplateSection("1 项目概述", 1, mode="ai", body_start_index=5)


@pytest.fixture
def flat_template_sections():
    return [
        TemplateSection("项目概述", 1, mode="ai", body_start_index=1),
        TemplateSection("测试策略", 1, mode="ai", body_start_index=10),
        TemplateSection("硬件环境", 1, mode="keep", body_start_index=20),
    ]


@pytest.fixture
def nested_template_sections():
    return [
        TemplateSection("1 概述", 1, mode="ai", body_start_index=1, children=[
            TemplateSection("1.1 背景", 2, mode="ai"),
            TemplateSection("1.2 目标", 2, mode="ai"),
        ]),
    ]


@pytest.fixture
def mixed_mode_sections():
    return [
        TemplateSection("项目概述", 1, mode="ai", body_start_index=1),
        TemplateSection("测试环境", 1, mode="keep", body_start_index=10),
        TemplateSection("审批信息", 1, mode="manual", body_start_index=20),
    ]


@pytest.fixture
def parse_result_with_sections():
    return TemplateParseResult(
        template_name="测试模板.docx",
        sections=[
            TemplateSection("1 概述", 1, body_start_index=1),
            TemplateSection("2 范围", 1, body_start_index=10, children=[
                TemplateSection("2.1 功能范围", 2, body_start_index=15),
            ]),
        ],
        table_count=5,
        fixed_approval_count=1,
    )


# ── OcrService tests ─────────────────────────────────────────────


class TestOcrService:
    def test_available(self):
        from app.common.ocr_service import ocr_service
        # PaddleOCR may or may not be installed; this just checks non-crash
        result = ocr_service.available
        assert isinstance(result, bool)

    def test_extract_nonexistent_file(self):
        from app.common.ocr_service import ocr_service
        with pytest.raises(FileNotFoundError):
            ocr_service.extract_text(Path("/nonexistent/image.png"))

    def test_extract_missing_file_returns_empty_when_unavailable(self, monkeypatch):
        """If OCR module is not available, extract_text returns '' gracefully."""
        from app.common.ocr_service import OcrService
        import app.common.ocr_service as ocr_mod
        svc = OcrService()
        # Patch the module-level function that the property delegates to
        monkeypatch.setattr(ocr_mod, "is_paddleocr_available", lambda: False)
        # Need a fresh instance since the property caches at instance level
        svc2 = OcrService()
        result = svc2.extract_text(Path(__file__))  # exists but not an image
        assert result == ""
