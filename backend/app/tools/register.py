"""Register all tools (F025 added TestPlanRegenTool) with the global ToolRegistry."""

from app.tools.registry import tool_registry
from app.tools.requirement_parser_tool import RequirementParserTool
from app.tools.template_parser_tool import TemplateParserTool
from app.tools.knowledge_search_tool import KnowledgeSearchTool
from app.tools.section_suggestion_tool import SectionSuggestionTool
from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
from app.tools.test_plan_regen_tool import TestPlanRegenTool
from app.tools.result_review_tool import ResultReviewTool
from app.tools.word_export_tool import WordExportTool
from app.tools.docx_format_check_tool import DocxFormatCheckTool


def register_all_tools() -> None:
    tool_registry.register(RequirementParserTool())
    tool_registry.register(TemplateParserTool())
    tool_registry.register(KnowledgeSearchTool())
    tool_registry.register(SectionSuggestionTool())
    tool_registry.register(TestPlanGeneratorTool())
    tool_registry.register(TestPlanRegenTool())
    tool_registry.register(ResultReviewTool())
    tool_registry.register(WordExportTool())
    tool_registry.register(DocxFormatCheckTool())
# tools.register:把 9 个 Tool(8 主工具 + TestPlanRegenTool)集中注册到全局 ToolRegistry;Agent 启动前调用一次。
