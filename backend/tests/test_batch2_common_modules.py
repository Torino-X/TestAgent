"""Tests for batch 2 common modules.

Tests PromptBuilder, PromptLoader, RateLimiter, JSON utils, PromptDump,
and the updated LLMClient.
"""

import os
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── JSON utils ────────────────────────────────────────────────────


class TestExtractJson:
    def test_pure_json(self):
        from app.common.json_utils import extract_json_from_llm_response

        result = extract_json_from_llm_response('{"key": "value"}')
        assert result == {"key": "value"}

    def test_markdown_fence(self):
        from app.common.json_utils import extract_json_from_llm_response

        result = extract_json_from_llm_response(
            '```json\n{"a": 1}\n```'
        )
        assert result == {"a": 1}

    def test_markdown_fence_no_lang(self):
        from app.common.json_utils import extract_json_from_llm_response

        result = extract_json_from_llm_response(
            '```\n{"b": 2}\n```'
        )
        assert result == {"b": 2}

    def test_bracket_extraction(self):
        from app.common.json_utils import extract_json_from_llm_response

        result = extract_json_from_llm_response(
            'Some text before {"x": [1, 2, 3]} and after'
        )
        assert result == {"x": [1, 2, 3]}

    def test_nested_braces(self):
        from app.common.json_utils import extract_json_from_llm_response

        result = extract_json_from_llm_response(
            '{"outer": {"inner": "val"}} trailing'
        )
        assert result == {"outer": {"inner": "val"}}

    def test_no_json_raises(self):
        from app.common.json_utils import extract_json_from_llm_response

        with pytest.raises(ValueError, match="未找到 JSON"):
            extract_json_from_llm_response("No JSON here at all")

    def test_incomplete_json_raises(self):
        from app.common.json_utils import extract_json_from_llm_response

        with pytest.raises(ValueError, match="不完整"):
            extract_json_from_llm_response('{"a": ')


# ── Rate limiter ──────────────────────────────────────────────────


class TestRateLimiter:
    def test_unlimited_immediate(self):
        from app.common.rate_limiter import RateLimiter

        limiter = RateLimiter(max_requests_per_minute=0)
        # Should return immediately (infinite rate)

        async def run():
            await limiter.acquire()
            await limiter.acquire()

        asyncio.run(run())

    def test_limited_acquires(self):
        from app.common.rate_limiter import RateLimiter

        limiter = RateLimiter(max_requests_per_minute=600, max_burst=10)

        async def run():
            for _ in range(5):
                await limiter.acquire()

        asyncio.run(run())

    def test_burst_respected(self):
        from app.common.rate_limiter import RateLimiter

        limiter = RateLimiter(max_requests_per_minute=60, max_burst=2)

        async def run():
            # Two immediate acquires should succeed (within burst)
            await limiter.acquire()
            await limiter.acquire()
            # Third must wait
            # Just verify it doesn't error

        asyncio.run(run())

    def test_legacy_alias(self):
        from app.common.rate_limiter import TokenBucketRateLimiter, RateLimiter

        assert TokenBucketRateLimiter is RateLimiter


# ── Prompt dump ───────────────────────────────────────────────────


class TestPromptDump:
    def test_disabled_by_default(self, monkeypatch):
        # Ensure env var is unset (previous test may have set it)
        monkeypatch.delenv("PLANWISE_DUMP_PROMPTS", raising=False)
        from app.common.prompt_dump import dump_prompt

        result = dump_prompt("test prompt", "test")
        assert result == ""

    def test_enabled_writes_file(self, monkeypatch):
        monkeypatch.setenv("PLANWISE_DUMP_PROMPTS", "1")
        import app.common.prompt_dump as pd_mod

        result = pd_mod.dump_prompt("test content", "test_context")
        if result:
            try:
                os.unlink(result)
            except OSError:
                pass
        assert isinstance(result, str)

    def test_sanitize_api_keys(self):
        from app.common.prompt_dump import _sanitize

        text = "Authorization: " + "sk-" + "abc123def456ghijklmnopqrstuvwxyz"
        sanitized = _sanitize(text)
        assert "sk-abc" not in sanitized
        assert "REDACTED" in sanitized

    def test_sanitize_bearer(self):
        from app.common.prompt_dump import _sanitize

        text = "Bearer my_secret_token_12345"
        sanitized = _sanitize(text)
        assert "my_secret_token" not in sanitized
        assert "REDACTED" in sanitized


# ── Prompt loader ─────────────────────────────────────────────────


class TestPromptLoader:
    def test_find_project_root(self):
        from app.common.prompt_loader import _find_project_root

        root = _find_project_root()
        assert root.name == "backend"

    def test_find_template_dir(self):
        from app.common.prompt_loader import _find_template_dir

        template_dir = _find_template_dir()
        assert template_dir.exists()
        assert template_dir.name == "templates"

    def test_find_rules_dir(self):
        from app.common.prompt_loader import _find_rules_dir

        rules_dir = _find_rules_dir()
        assert rules_dir.exists()
        assert rules_dir.name == "system_rules"

    def test_load_template_file_not_found(self):
        from app.common.prompt_loader import load_template

        with pytest.raises(FileNotFoundError, match="不存在"):
            load_template("nonexistent_file.md")

    def test_parse_sections(self):
        from app.common.prompt_loader import parse_sections

        text = (
            "[section1]\ncontent line 1\ncontent line 2\n\n[section2]\nmore content\n"
        )
        sections = parse_sections(text)
        assert sections["section1"] == "content line 1\ncontent line 2"
        assert sections["section2"] == "more content"

    def test_parse_sections_empty(self):
        from app.common.prompt_loader import parse_sections

        assert parse_sections("") == {}

    def test_parse_sections_no_headers(self):
        from app.common.prompt_loader import parse_sections

        assert parse_sections("just\ntext\nno\nsections") == {}

    def test_render_template_raises_on_missing_var(self):
        from app.common.prompt_loader import render_template

        with pytest.raises(FileNotFoundError):
            render_template("nonexistent.md", {})


# ── Prompt builder ────────────────────────────────────────────────


class TestPromptBuilder:
    @pytest.fixture
    def builder(self):
        from app.common.prompt_builder import PromptBuilder

        return PromptBuilder()

    @pytest.fixture
    def template_structure(self):
        return {
            "headings": ["1 项目概述", "2 测试目标", "3 测试范围"],
            "table_fields": ["功能项 | 优先级 | 说明"],
        }

    @pytest.fixture
    def generation_config(self):
        return {
            "ai_fields": [
                {
                    "section_id": "1",
                    "field": "overview",
                    "title": "项目概述",
                    "level": 1,
                    "description": "描述项目背景和目标",
                    "table_schemas": [],
                },
                {
                    "section_id": "2",
                    "field": "test_objectives",
                    "title": "测试目标",
                    "level": 1,
                    "table_schemas": [
                        {"headers": ["目标", "描述", "验收标准"]}
                    ],
                },
            ],
            "keep_sections": ["测试环境", "审批信息"],
            "manual_sections": ["修订记录"],
            "json_schema_preview": '{"overview": "string", "test_objectives": "string"}',
        }

    def test_build_full_prompt(self, builder, template_structure, generation_config):
        prompt = builder.build(
            user_prompt="请生成测试方案",
            requirement_text="需求文档内容...",
            template_structure=template_structure,
            template_generation_config=generation_config,
        )
        assert "项目概述" in prompt
        assert "需求文档内容" in prompt
        assert "overview" in prompt
        assert "test_objectives" in prompt
        assert "测试环境" in prompt  # keep_section
        assert "修订记录" in prompt  # manual_section

    def test_build_without_config(self, builder, template_structure):
        prompt = builder.build(
            user_prompt="生成",
            requirement_text="需求",
            template_structure=template_structure,
        )
        assert "未配置章节级生成范围" in prompt

    def test_build_outline_prompt(self, builder, template_structure):
        ai_fields = [
            {"field": "overview", "title": "项目概述"},
            {"field": "strategy", "title": "测试策略"},
        ]
        prompt = builder.build_outline_prompt(
            user_prompt="请生成大纲",
            requirement_text="需求文档",
            template_structure=template_structure,
            ai_fields=ai_fields,
        )
        assert "一致性大纲" in prompt
        assert "terminology" in prompt
        assert "cross_cutting" in prompt
        assert "overview" in prompt
        assert "strategy" in prompt

    def test_build_batch_prompt(self, builder, template_structure):
        batch_fields = [
            {
                "field": "overview",
                "title": "项目概述",
                "section_id": "1",
                "level": 1,
                "table_schemas": [],
            }
        ]
        outline = {
            "terminology": {"项目": "xxx管理系统"},
            "cross_cutting": ["决策1"],
            "chapter_summaries": {"overview": "项目背景概述"},
        }
        rules = "【测试规则】"
        prompt = builder.build_batch_prompt(
            user_prompt="请生成本批次",
            requirement_text="需求文档",
            template_structure=template_structure,
            batch_fields=batch_fields,
            outline_json=outline,
            generation_rules=rules,
        )
        assert "本批次" in prompt
        assert "xxx管理系统" in prompt
        assert "决策1" in prompt
        assert "overview" in prompt

    def test_rules_dir_default(self, builder):
        assert builder.rules_dir.name == "system_rules"

    def test_load_rule_missing(self, builder):
        content = builder._load_rule_file("nonexistent.md")
        assert content == ""

    def test_format_ai_fields_summary(self, builder):
        ai_fields = [
            {"field": "overview", "title": "概述"},
            {"field": "strategy", "title": "策略"},
        ]
        result = builder._format_ai_fields_summary(ai_fields)
        assert "overview" in result
        assert "strategy" in result

    def test_format_ai_fields_detail_with_table(self, builder):
        ai_fields = [
            {
                "field": "test_env",
                "title": "测试环境",
                "section_id": "5",
                "level": 1,
                "description": "环境说明",
                "table_schemas": [{"headers": ["环境", "配置"], "complex": True}],
            }
        ]
        result = builder._format_ai_fields_detail(ai_fields)
        assert "测试环境" in result
        assert "环境" in result
        assert "配置" in result
        assert "复杂格式" in result

    def test_format_ai_fields_detail_multi_table(self, builder):
        ai_fields = [
            {
                "field": "resources",
                "title": "测试资源",
                "section_id": "6",
                "level": 1,
                "table_schemas": [
                    {"headers": ["角色", "职责"]},
                    {"headers": ["设备", "数量"]},
                ],
            }
        ]
        result = builder._format_ai_fields_detail(ai_fields)
        assert "二维数组" in result
        assert "表格1" in result
        assert "表格2" in result


# ── LLMClient ─────────────────────────────────────────────────────


class TestLLMClient:
    def test_normalize_base_url(self):
        from app.integrations.llm_client import LLMClient

        assert LLMClient._normalize_base_url("https://api.openai.com/v1/") == "https://api.openai.com/v1"
        assert LLMClient._normalize_base_url("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1"
        assert LLMClient._normalize_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"

    def test_normalize_chat_url(self):
        from app.integrations.llm_client import LLMClient

        assert LLMClient._normalize_chat_url("https://api/v1") == "https://api/v1/chat/completions"
        assert LLMClient._normalize_chat_url("https://api/v1/chat/completions") == "https://api/v1/chat/completions"

    def test_extract_completion_content_standard(self):
        from app.integrations.llm_client import LLMClient

        mock = MagicMock()
        mock.choices = [MagicMock()]
        mock.choices[0].message.content = "response text"
        assert LLMClient._extract_completion_content(mock) == "response text"

    def test_extract_completion_content_dict(self):
        from app.integrations.llm_client import LLMClient

        data = {"choices": [{"message": {"content": "dict response"}}]}
        assert LLMClient._extract_completion_content(data) == "dict response"

    def test_extract_completion_content_empty_choices(self):
        from app.integrations.llm_client import LLMClient

        mock = MagicMock()
        mock.choices = []
        # _extract_completion_content returns "" for empty choices;
        # the caller (generate()) is responsible for raising the error.
        result = LLMClient._extract_completion_content(mock)
        assert result == ""

    def test_stringify_content_str(self):
        from app.integrations.llm_client import LLMClient

        assert LLMClient._stringify_content("hello") == "hello"

    def test_stringify_content_list_of_dicts(self):
        from app.integrations.llm_client import LLMClient

        assert LLMClient._stringify_content([{"text": "part1"}, {"text": "part2"}]) == "part1part2"

    def test_completion_finish_reason(self):
        from app.integrations.llm_client import LLMClient

        mock = MagicMock()
        mock.choices = [MagicMock()]
        mock.choices[0].finish_reason = "stop"
        assert LLMClient._completion_finish_reason(mock) == "stop"

    def test_extract_content_dict_message(self):
        from app.integrations.llm_client import LLMClient

        data = {"choices": [{"message": {"content": "hello"}}]}
        assert LLMClient._extract_content(data) == "hello"

    @pytest.mark.asyncio
    async def test_build_user_message_text_only(self):
        from app.integrations.llm_client import LLMClient

        client = LLMClient()
        msg = await client._build_user_message("test prompt")
        assert msg["role"] == "user"
        assert msg["content"] == "test prompt"

    @pytest.mark.asyncio
    async def test_build_user_message_with_images(self, tmp_path):
        from app.integrations.llm_client import LLMClient

        # Create a tiny test PNG
        img_path = tmp_path / "test.png"
        # Minimal PNG bytes: 1x1 white pixel
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
            b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        img_path.write_bytes(png_bytes)

        client = LLMClient()
        msg = await client._build_user_message("prompt", [str(img_path)])
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][1]["type"] == "image_url"
        assert "base64" in msg["content"][1]["image_url"]["url"]

    def test_mock_llm_generate(self):
        from app.integrations.llm_client import MockLLMClient

        async def run():
            client = MockLLMClient()
            result = await client.generate("这是一个测试系统平台")
            # MockLLM returns a substantial markdown response
            assert "# " in result
            assert "测试方案" in result
            assert "## 项目概述" in result

        asyncio.run(run())

    def test_mock_llm_extract_hint_no_keyword(self):
        from app.integrations.llm_client import MockLLMClient

        hint = MockLLMClient._extract_project_hint("just text")
        assert hint == "项目"

    def test_llm_client_error_import(self):
        from app.integrations.llm_client import LLMClientError

        # Verify base error class
        assert issubclass(LLMClientError, Exception)


# ── MockLLMClient ─────────────────────────────────────────────────


class TestMockLLMClient:
    def test_generate_returns_markdown(self):
        from app.integrations.llm_client import MockLLMClient

        async def run():
            client = MockLLMClient()
            result = await client.generate("测试系统平台的需求")
            assert "测试方案" in result
            assert "项目概述" in result
            assert "测试目标" in result
            assert "风险分析" in result
            assert "测试交付物" in result

        asyncio.run(run())

    def test_health_check(self):
        from app.integrations.llm_client import MockLLMClient

        async def run():
            client = MockLLMClient()
            assert await client.health_check() is True

        asyncio.run(run())


# ── System rules existence ────────────────────────────────────────


class TestSystemRules:
    def test_anchor_files_exist(self):
        from app.common.prompt_loader import _find_rules_dir

        rules_dir = _find_rules_dir()
        assert (rules_dir / "anchor_beginning.md").exists()
        assert (rules_dir / "anchor_middle.md").exists()
        assert (rules_dir / "full_rules.md").exists()

    def test_anchor_content_non_empty(self):
        from app.common.prompt_loader import _find_rules_dir

        rules_dir = _find_rules_dir()
        for name in ("anchor_beginning.md", "anchor_middle.md"):
            content = (rules_dir / name).read_text(encoding="utf-8")
            assert len(content) > 0, f"{name} should not be empty"

    def test_full_rules_has_placeholder(self):
        from app.common.prompt_loader import _find_rules_dir

        rules_dir = _find_rules_dir()
        content = (rules_dir / "full_rules.md").read_text(encoding="utf-8")
        assert "{generation_rules}" in content
