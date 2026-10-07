"""Timeout resolution priority for LLMClient.

Phase 2.9B.13 (timeout user-config priority): 用户在「模型设置」中
保存的 ``timeout_seconds`` 必须作为主模型调用的基础超时,专用 Profile
(``test_plan_generation``) 可以提供默认值, 但不能无条件覆盖用户
显式配置。完整优先级:

    调用级显式覆盖 (timeout_override)
    → 用户模型配置 timeout_seconds (DB → LLMConfigProvider.timeout)
    → Profile 默认 timeout
    → 系统安全默认值

测试覆盖 10 个场景:

1. 用户配置 300s 后, 调用解析为 300 (不降级)。
2. 缓存刷新后, TestPlanGeneratorTool 读取的 user_timeout = 300。
3. Profile 默认 90 不能覆盖用户 300。
4. 用户未配置时 (cfg.timeout = None), Profile 默认 90 生效。
5. 显式调用级 timeout 可以按现有规则覆盖用户。
6. 超过系统硬上限 (300) 会被压到上限, 同时 WARN。
7. 不存在额外 ``asyncio.wait_for(90)`` 提前终止 (静态扫描)。
8. 保存配置后无需重启即可生效 (cache 立即刷新)。
9. 重启后仍读取 300 (cache miss → loader 读 DB → 300)。
10. 其他短任务 Profile (TITLE) 不受 _DEFAULT_TEST_PLAN_TIMEOUT 干扰。

诊断日志字段 (user_configured_timeout / profile_default_timeout /
request_override_timeout / effective_timeout / timeout_source /
cache_hit) 也被验证。
"""

from __future__ import annotations

import asyncio
import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 直接 import 解析器 ────────────────────────────────────────────

from app.integrations.llm_client import (
    _DEFAULT_TEST_PLAN_TIMEOUT,
    _TEST_PLAN_SYSTEM_PROMPT,
    _DEFAULT_TIMEOUT_SECONDS,
    _MAX_TIMEOUT_SECONDS,
    _resolve_effective_timeout,
)


_TEST_PLAN_PROMPT_PREFIX = _TEST_PLAN_SYSTEM_PROMPT  # 实际就是 _DEFAULT_TEST_PLAN_SYSTEM_PROMPT_PREFIX


# ── Helpers ──────────────────────────────────────────────────────


def _cfg(timeout=None, **extra):
    """构造最小可用的 cfg 对象(类似 LLMConfigProvider)。"""
    return SimpleNamespace(api_url="https://x", api_key="k", model_name="m", timeout=timeout, **extra)


# ── 1. 用户配置 300s 后, 解析为 300 ─────────────────────────────


class TestUserConfig300:
    def test_user_300_wins(self):
        """场景 1: 用户在「模型设置」保存 300, 解析器返回 300。"""
        cfg = _cfg(timeout=300)
        effective, debug = _resolve_effective_timeout(
            cfg, system_prompt=_TEST_PLAN_PROMPT_PREFIX, request_override=None
        )
        assert effective == 300
        assert debug["user_configured_timeout"] == 300
        assert debug["profile_default_timeout"] == 90
        assert debug["request_override_timeout"] is None
        assert debug["timeout_source"] == "user_model_config"

    def test_user_300_above_profile_90(self):
        """场景 3: Profile 默认 90 不能覆盖用户 300。"""
        cfg = _cfg(timeout=300)
        effective, _ = _resolve_effective_timeout(
            cfg, system_prompt=_TEST_PLAN_PROMPT_PREFIX, request_override=None
        )
        assert effective == 300, "Profile 90 must NOT override user 300"


# ── 2. Profile 默认 90 在用户未配置时生效 ──────────────────────────


class TestProfileDefault:
    def test_no_user_config_uses_profile_90(self):
        """场景 4: 用户未配置 (cfg.timeout = None) 时, Profile 默认 90。"""
        cfg = _cfg(timeout=None)
        effective, debug = _resolve_effective_timeout(
            cfg, system_prompt=_TEST_PLAN_PROMPT_PREFIX, request_override=None
        )
        assert effective == 90
        assert debug["user_configured_timeout"] is None
        assert debug["profile_default_timeout"] == 90
        assert debug["timeout_source"] == "profile_default"

    def test_user_zero_treated_as_unset(self):
        """用户把 timeout 存成 0 (DB 偶尔出现), 视为未配置。"""
        cfg = _cfg(timeout=0)
        effective, debug = _resolve_effective_timeout(
            cfg, system_prompt=_TEST_PLAN_PROMPT_PREFIX, request_override=None
        )
        assert effective == 90
        assert debug["timeout_source"] == "profile_default"

    def test_non_test_plan_prompt_no_profile_default(self):
        """场景 10: TITLE 等其他 Profile 不应触发 _DEFAULT_TEST_PLAN_TIMEOUT。"""
        cfg = _cfg(timeout=None)
        # 短任务 TITLE 的 system_prompt 与 _TEST_PLAN_SYSTEM_PROMPT 不同
        effective, debug = _resolve_effective_timeout(
            cfg, system_prompt="你是一个标题生成器", request_override=None
        )
        # 没有 profile default → 走 system_default
        assert effective == _DEFAULT_TIMEOUT_SECONDS
        assert debug["profile_default_timeout"] is None
        assert debug["timeout_source"] == "system_default"


# ── 3. 调用级 timeout_override 优先于用户配置 ──────────────────────


class TestRequestOverride:
    def test_override_wins_over_user(self):
        """场景 5: 显式调用级 timeout 可以按现有规则覆盖用户。"""
        cfg = _cfg(timeout=300)
        effective, debug = _resolve_effective_timeout(
            cfg, system_prompt=_TEST_PLAN_PROMPT_PREFIX, request_override=45
        )
        assert effective == 45
        assert debug["request_override_timeout"] == 45
        assert debug["timeout_source"] == "request_override"

    def test_override_zero_is_treated_as_request_override(self):
        """调用级 timeout=0 是显式值(不是 None-equivalent), 走 request_override
        分支. 此处 user=300 仍被覆盖 → 0; 上层 (LLMClient 调用方) 应避免传 0."""
        cfg = _cfg(timeout=300)
        effective, debug = _resolve_effective_timeout(
            cfg, system_prompt=_TEST_PLAN_PROMPT_PREFIX, request_override=0
        )
        assert effective == 0
        assert debug["timeout_source"] == "request_override"


# ── 4. 系统硬上限 ───────────────────────────────────────────────


class TestHardCeiling:
    def test_above_max_clamped_with_warning(self, caplog):
        """场景 6: 超过系统硬上限 (300) 会被压到上限, 同时 WARN。"""
        cfg = _cfg(timeout=9999)
        import logging
        with caplog.at_level(logging.WARNING):
            effective, debug = _resolve_effective_timeout(
                cfg, system_prompt=_TEST_PLAN_PROMPT_PREFIX, request_override=None
            )
        assert effective == _MAX_TIMEOUT_SECONDS
        assert effective == 300
        assert any(
            "超过系统硬上限" in rec.message for rec in caplog.records
        ), f"Expected WARN log, got: {[r.message for r in caplog.records]}"


# ── 5. 静态扫描: 不存在 asyncio.wait_for(90) 提前终止 ─────────────


class TestNoHardcodedAsyncioTimeout:
    def test_no_asyncio_wait_for_in_llm_client(self):
        """场景 7: LLMClient 文件中不应硬编码 asyncio.wait_for(90)。"""
        import app.integrations.llm_client as mod
        source = inspect.getsource(mod)
        # 检查是否有 asyncio.wait_for(...) / asyncio.timeout(...) 的硬编码
        bad = re.findall(r"asyncio\.(?:wait_for|timeout)\(\s*\d", source)
        assert not bad, (
            f"发现硬编码 asyncio 限制: {bad}. LLMClient 不应有外层 "
            f"asyncio.wait_for/asyncio.timeout 提前终止."
        )

    def test_no_magic_number_90_remaining(self):
        """_DEFAULT_TEST_PLAN_TIMEOUT = 90 是允许的 (Profile 兜底),
        但不应再出现裸的 ``timeout = 90`` 强制覆盖用户配置."""
        import app.integrations.llm_client as mod
        source = inspect.getsource(mod)
        # 寻找 _do_chat_completion 函数体内"裸"赋值 timeout = 90
        # 允许出现 _DEFAULT_TEST_PLAN_TIMEOUT = 90 这种常量定义
        assert "timeout = 90" not in source, (
            "LLMClient 中不应再出现 `timeout = 90` 这种硬编码覆盖。"
            "Profile default 应该走 _DEFAULT_TEST_PLAN_TIMEOUT。"
        )


# ── 6. 缓存刷新: 保存后立即生效 ──────────────────────────────────


class TestCacheRefresh:
    async def test_settings_service_invalidates_and_reloads_cache(self):
        """场景 8: 保存配置后无需重启即可生效, 验证 cache 写入新 timeout."""
        from app.core.llm_config_cache import llm_config_cache
        from app.services.settings_service import LLMConfigProvider

        # Reset cache to a known state
        await llm_config_cache.clear()

        # 模拟 bootstrap 后: cache 里有 300
        provider_old = LLMConfigProvider(
            api_url="https://x", api_key="k", model_name="m", timeout=300,
        )
        await llm_config_cache.set(42, provider_old)
        cached = await llm_config_cache.get_or_load(42, lambda uid: asyncio.sleep(0))
        assert cached is not None
        assert cached.timeout == 300, "cache 写后立即可读 300"

        # 用户改为 200 → 写 cache
        provider_new = LLMConfigProvider(
            api_url="https://x", api_key="k", model_name="m", timeout=200,
        )
        await llm_config_cache.set(42, provider_new)
        cached2 = await llm_config_cache.get_or_load(42, lambda uid: asyncio.sleep(0))
        assert cached2.timeout == 200, "保存后立即生效, 无需重启"

        await llm_config_cache.clear()


# ── 7. 重启后仍读取 (loader 走 DB) ───────────────────────────────


class TestAfterRestart:
    async def test_loader_returns_user_timeout(self):
        """场景 9: 重启后 cache miss, loader 从 DB 读取, timeout = 300."""
        from app.core.llm_config_cache import llm_config_cache
        from app.services.settings_service import LLMConfigProvider

        await llm_config_cache.clear()

        async def fake_loader(uid):
            # 模拟 DB 行, timeout=300
            return LLMConfigProvider(
                api_url="https://x", api_key="k", model_name="m", timeout=300,
            )

        provider = await llm_config_cache.get_or_load(7, fake_loader)
        assert provider is not None
        assert provider.timeout == 300, "重启后 cache miss → loader → 300"

        await llm_config_cache.clear()


# ── 8. 端到端: LLMClient 实际取到 300 ─────────────────────────────


class TestEndToEndTimeout300:
    async def test_do_chat_completion_uses_user_300(self, monkeypatch, caplog):
        """场景 2/3/5: TestPlanGeneratorTool 走 generate() → _do_chat_completion,
        验证:
        - user_configured_timeout=300
        - effective_timeout=300
        - timeout_source=user_model_config
        - AsyncOpenAI(timeout=300, ...)
        """
        from app.integrations import llm_client as mod

        # 模拟 _TEST_PLAN_SYSTEM_PROMPT 起点
        sys_prompt = _TEST_PLAN_PROMPT_PREFIX + "请生成测试方案"

        # 拦截 AsyncOpenAI, 记录 timeout
        captured = {}

        class FakeCompletions:
            async def create(self, **kwargs):
                # 不返回有效内容, 让 _do_chat_completion 抛 LLMClientError
                # 我们只关心 timeout 是否被正确传
                raise mod.LLMClientError("stop-after-timeout-capture")

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.chat = SimpleNamespace(completions=FakeCompletions())

        # patch openai 的 import
        import sys
        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeOpenAI))

        provider = SimpleNamespace(
            api_url="https://x", api_key="k", model_name="m",
            timeout=300, enable_thinking=False, max_tokens=None, temperature=None,
            is_unconfigured=False,
        )
        client = mod.LLMClient(config_provider=provider)
        # 跳过 rate_limit / message build
        monkeypatch.setattr(client, "_resolve_api_key", lambda c: c.api_key)
        monkeypatch.setattr(client, "_build_user_message", AsyncMock(return_value={"role": "user", "content": "x"}))

        import logging
        with caplog.at_level(logging.INFO, logger="app.integrations.llm_client"):
            try:
                await client.generate("prompt")
            except mod.LLMClientError:
                pass

        # 关键断言
        assert captured.get("timeout") == 300, (
            f"AsyncOpenAI timeout 应为 300, 实际 {captured.get('timeout')}. "
            f"说明 90 秒硬编码仍然生效, 修复未成功."
        )
        # 诊断日志
        msgs = [r.message for r in caplog.records if "主模型调用开始" in r.message]
        assert msgs, f"主模型调用开始 日志未出现: {[r.message for r in caplog.records][:5]}"
        msg = msgs[0]
        assert "user_configured_timeout=300" in msg, f"日志缺 user_configured_timeout=300: {msg}"
        assert "effective_timeout=300" in msg, f"日志缺 effective_timeout=300: {msg}"
        assert "timeout_source=user_model_config" in msg, f"日志缺 timeout_source: {msg}"
        # 确保不再出现 90
        assert "effective_timeout=90" not in msg, f"日志仍显示 90: {msg}"


# ── 9. 完整优先级的"端到端表格" (4 个组合) ─────────────────────────


class TestPriorityTable:
    """一表说清优先级:

    | user  | profile | override | expect       | source                |
    |-------|---------|----------|--------------|-----------------------|
    | 300   | 90      | None     | 300          | user_model_config     |
    | None  | 90      | None     | 90           | profile_default       |
    | 300   | 90      | 45       | 45           | request_override      |
    | 9999  | 90      | None     | 300 (clamp)  | user_model_config     |
    """

    @pytest.mark.parametrize(
        "user,profile_prompt,override,expected,source",
        [
            (300, True,  None, 300, "user_model_config"),
            (None, True,  None, 90,  "profile_default"),
            (300, True,  45,   45,  "request_override"),
            (9999,True,  None, 300, "user_model_config"),
        ],
    )
    def test_priority_table(self, user, profile_prompt, override, expected, source):
        cfg = _cfg(timeout=user)
        sys_prompt = (
            _TEST_PLAN_PROMPT_PREFIX
            if profile_prompt
            else "其他 system prompt"
        )
        effective, debug = _resolve_effective_timeout(cfg, sys_prompt, override)
        assert effective == expected
        assert debug["timeout_source"] == source
