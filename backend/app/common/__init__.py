"""Common modules adapted from legacy ai_test_plan_generator.

This package holds business-logic modules that were extracted from the
legacy PyQt5 desktop application and adapted for the FastAPI/async
TestAgent backend.  Every module in here must be free of:

- PyQt5 / QML dependencies
- Word COM automation
- Windows-only path helpers
- Direct local filesystem access (use local_storage instead)
- Synchronous blocking I/O on the event loop (use asyncio.to_thread)
"""

from app.common.document_reader import DocumentReader, DocumentReadError, DocumentReadResult, RequirementBlock
from app.common.json_utils import extract_json_from_llm_response
from app.common.prompt_builder import PromptBuilder
from app.common.prompt_dump import dump_prompt
from app.common.prompt_loader import load_template, render_template, load_section, parse_sections
from app.common.rate_limiter import RateLimiter, TokenBucketRateLimiter
from app.common.result_parser import ResultParser, ResultParseError, JsonValidationResult
from app.common.template_section import TemplateSection, TemplateParseResult
from app.common.template_section_service import TemplateSectionService
from app.common.template_prompt_preview_service import TemplatePromptPreviewService
from app.common.test_plan_schema import DEFAULT_SECTIONS, CompletenessResult
from app.common.word_exporter import WordExporter, WordExportError

__all__ = [
    # document_reader
    "DocumentReader",
    "DocumentReadError",
    "DocumentReadResult",
    "RequirementBlock",
    # json_utils
    "extract_json_from_llm_response",
    # prompt_builder
    "PromptBuilder",
    # prompt_dump
    "dump_prompt",
    # prompt_loader
    "load_template",
    "render_template",
    "load_section",
    "parse_sections",
    # rate_limiter
    "RateLimiter",
    "TokenBucketRateLimiter",
    # result_parser
    "ResultParser",
    "ResultParseError",
    "JsonValidationResult",
    # template_section
    "TemplateSection",
    "TemplateParseResult",
    "TemplateSectionService",
    "TemplatePromptPreviewService",
    # test_plan_schema
    "DEFAULT_SECTIONS",
    "CompletenessResult",
    # word_exporter
    "WordExporter",
    "WordExportError",
]
# common 子包:横切工具(JSON/格式化/token 估算/Word 导出/模板解析 等)。
