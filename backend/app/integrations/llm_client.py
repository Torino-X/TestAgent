"""LLM client — adapted from legacy llm_client.py.

OpenAI-compatible chat completion client with:
- Async API (httpx-based, configured at call time from DB settings)
- Four-category error classification (timeout / connection / status / unknown)
- JSON continuation (truncation recovery)
- Image support (base64 data URLs)
- Token-bucket rate limiting (async-safe)
- Prompt dump for debugging (sanitised)
- MockLLMClient for testing

Equivalent migration: all 19 capabilities preserved.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from app.common.prompt_dump import dump_prompt
from app.common.rate_limiter import RateLimiter
from app.core.logging import LogEvent, log_event

logger = logging.getLogger(__name__)


# ── Module constants ─────────────────────────────────────────────


# System prompt used by ``LLMClient.generate`` (legacy entry point).
# TestPlanGeneratorTool and WordExportTool both call generate() and rely on
# this prompt to constrain the model to "test plan assistant" behaviour.
# When adding a new entry point with a different persona, use
# ``generate_with_system`` instead so callers can supply their own prompt.
_TEST_PLAN_SYSTEM_PROMPT = (
    "你是一名严谨的项目测试方案生成助手，"
    "必须严格遵循用户给定的模板结构和输出格式。"
)

# 2026-07：默认 max_tokens 与测试方案生成的协议级兜底。
# 当用户在「模型设置」里把 ``max_tokens`` 留空（DB 存 NULL）时，
# ``cfg.max_tokens`` 就是 None；按调用方启发式给一个合理的默认值，
# 避免依赖 provider 自身（有的 provider 默认 4096，会把 23k 字符的
# 测试方案响应截到一半）。当前所有其它调用点（CHAT/INTENT/TITLE/
# SUMMARY）的 max_tokens 都很小，保持 None（让 provider 决定）。
_DEFAULT_TEST_PLAN_MAX_TOKENS = 16000

# 2026-07：默认 temperature 协议级兜底。原因与 max_tokens 同：
# ``TEST_PLAN_PROFILE.temperature=0.2`` 是死配置（TestPlanGeneratorTool
# 走 ``generate()``，profile 不会被读），而 provider 默认通常是 1.0。
# 高温度下 JSON 结构化输出稳定性塌方——同一段 prompt 跑两次可能
# 给出完全不同的章节切分。0.2 是经验上 JSON 生成任务的甜区：
# 足够随机以避免复读，又足够稳定让 schema 校验能通过。
_DEFAULT_TEST_PLAN_TEMPERATURE = 0.2

# 2026-07：协议级默认 timeout 兜底。
# 当用户在「模型设置」里把 ``timeout_seconds`` 留 0/None 时，cache
# 兜底给 120。系统级硬上限（防呆）：300 秒。超过这个值会被压到
# 这个上限，同时日志告警。完整优先级：
#   1. 调用级 timeout_override（generate_with_system / stream_with_system）
#   2. 用户模型配置 timeout_seconds（DB → LLMConfigProvider.timeout）
#   3. Profile 协议级 default（test_plan_generation=90, title=20 等）
#   4. 系统硬上限（300）
_DEFAULT_TIMEOUT_SECONDS = 120
_MAX_TIMEOUT_SECONDS = 300
_DEFAULT_TEST_PLAN_TIMEOUT = 90  # 历史 profile 兜底; 仅在用户未配置时生效


def _resolve_max_tokens(
    cfg: Any,
    system_prompt: str,
) -> Optional[int]:
    """Resolve effective ``max_tokens`` for a chat-completion call.

    Resolution order:
    1. ``cfg.max_tokens`` — what the user set in 模型设置 (highest priority).
    2. Protocol-level fallback for test_plan generation (16000) when
       the system prompt matches ``_TEST_PLAN_SYSTEM_PROMPT``.
    3. ``None`` — defer to the upstream provider's own default.

    The reason we don't just always trust ``cfg.max_tokens``: many users
    leave it NULL, and provider defaults (often 4096) silently truncate
    23k-character Chinese test-plan responses.  Without this resolver,
    bumping ``TEST_PLAN_PROFILE.max_tokens`` would be pure decoration.
    """
    cfg_max = getattr(cfg, "max_tokens", None)
    if cfg_max:
        return int(cfg_max)
    if system_prompt.strip().startswith(_TEST_PLAN_SYSTEM_PROMPT):
        return _DEFAULT_TEST_PLAN_MAX_TOKENS
    return None


def _resolve_temperature(
    cfg: Any,
    system_prompt: str,
) -> Optional[float]:
    """Resolve effective ``temperature`` for a chat-completion call.

    Same priority model as ``_resolve_max_tokens``:
    1. ``cfg.temperature`` — what the user set in 模型设置.
    2. Protocol-level fallback (0.2) for test_plan generation when the
       system prompt matches ``_TEST_PLAN_SYSTEM_PROMPT``.
    3. ``None`` — defer to the upstream provider's own default.

    Why we don't blanket-set temperature=0.2 on every call: chat /
    intent / title / summary each have their own tuned value, and the
    user can already override per-config.  Only test_plan needs the
    protocol-level safety net because (a) it's the only call site
    without a working profile-driven path, and (b) high temperature
    on structured JSON output is the most destructive failure mode.
    """
    cfg_temp = getattr(cfg, "temperature", None)
    if cfg_temp is not None:
        return float(cfg_temp)
    if system_prompt.strip().startswith(_TEST_PLAN_SYSTEM_PROMPT):
        return _DEFAULT_TEST_PLAN_TEMPERATURE
    return None


def _resolve_effective_timeout(
    cfg: Any,
    system_prompt: str,
    request_override: Optional[int],
) -> tuple[int, dict]:
    """Resolve the effective HTTP timeout for a chat-completion call.

    Priority (highest first):
        1. ``request_override`` — explicit caller-supplied timeout.
        2. ``cfg.timeout`` — what the user set in 模型设置 (DB-backed
           ``model_configs.timeout_seconds``).
        3. Profile 协议级 default — for test_plan_generation, the
           historical 90s safety net.  ONLY applies when the user has
           not configured an explicit value (i.e. ``cfg.timeout`` is
           falsy or at the protocol default 120).
        4. 系统硬上限 — clamp to ``_MAX_TIMEOUT_SECONDS`` (300) with a
           WARNING log when the resolved value exceeds it.

    Returns ``(effective_timeout, debug_fields)`` where ``debug_fields``
    is a dict containing the four key/value pairs the call site logs:

        user_configured_timeout
        profile_default_timeout
        request_override_timeout
        effective_timeout
        timeout_source
        cache_hit
    """
    user_timeout = getattr(cfg, "timeout", None)
    try:
        user_timeout_int = int(user_timeout) if user_timeout else None
    except (TypeError, ValueError):
        user_timeout_int = None

    # Treat the "no user config" case as None so the profile default can
    # fill in.  ``user_timeout_int is None`` is the canonical signal.
    profile_default = (
        _DEFAULT_TEST_PLAN_TIMEOUT
        if system_prompt.strip().startswith(_TEST_PLAN_SYSTEM_PROMPT)
        else None
    )

    if request_override is not None:
        effective = int(request_override)
        source = "request_override"
    elif user_timeout_int is not None and user_timeout_int > 0:
        # 用户在「模型设置」里设了非零值, 视为显式配置, 优先使用
        effective = user_timeout_int
        source = "user_model_config"
    elif profile_default is not None:
        # 用户未配置, 用 Profile 兜底
        effective = profile_default
        source = "profile_default"
    else:
        # 协议级默认
        effective = _DEFAULT_TIMEOUT_SECONDS
        source = "system_default"

    # 防呆: 超过系统硬上限时压到上限并 WARN
    if effective > _MAX_TIMEOUT_SECONDS:
        logger.warning(
            "LLMClient._resolve_effective_timeout: 解析值=%ds 超过系统硬上限=%ds, 已压到上限",
            effective, _MAX_TIMEOUT_SECONDS,
        )
        effective = _MAX_TIMEOUT_SECONDS

    debug = {
        "user_configured_timeout": user_timeout_int,
        "profile_default_timeout": profile_default,
        "request_override_timeout": request_override,
        "effective_timeout": effective,
        "timeout_source": source,
    }
    return effective, debug

# ── Error hierarchy ───────────────────────────────────────────────


class LLMClientError(Exception):
    """Base error for LLM client operations."""


# ── Async LLM client ──────────────────────────────────────────────


class LLMClient:
    """OpenAI-compatible async chat completion client.

    Configuration (api_url, api_key, model_name, timeout, enable_thinking)
    is resolved at call time from a config provider so that settings
    changes take effect without restarting the server.

    Usage::

        client = LLMClient(config_provider=settings_service)
        answer = await client.generate(prompt)
    """

    def __init__(
        self,
        config_provider: Any = None,
        max_requests_per_minute: int = 0,
        max_burst: int = 3,
    ):
        """
        Args:
            config_provider: An object with attributes ``api_url``, ``api_key``,
                ``model_name``, ``timeout``, ``enable_thinking``.  Typically a
                ``SettingsService`` instance or a simple config object.
            max_requests_per_minute: Rate limit (0 = unlimited).
            max_burst: Maximum burst size for the token bucket.
        """
        self._config_provider = config_provider
        self._rate_limiter = RateLimiter(
            max_requests_per_minute=max_requests_per_minute,
            max_burst=max_burst,
        )

    # ── Primary API ────────────────────────────────────────────────

    async def generate(
        self,
        prompt: str,
        images: Optional[List[str]] = None,
    ) -> str:
        """Send a chat completion request and return the response text.

        Backed by the hardcoded test-plan system prompt. New callers
        (IntentRouter, ChatLLMService) that need a different persona
        should use ``generate_with_system`` instead.

        Args:
            prompt: The user message text.
            images: Optional list of image file paths to attach as base64.

        Returns:
            The model's text response.

        Raises:
            LLMClientError: On any failure (timeout, connection, API status,
                or unexpected error).
        """
        user_message = await self._build_user_message(prompt, images)
        return await self._do_chat_completion(
            system_prompt=_TEST_PLAN_SYSTEM_PROMPT,
            user_message=user_message,
        )

    async def generate_with_system(
        self,
        system_prompt: str,
        user_content: str,
        images: Optional[List[str]] = None,
        *,
        model_override: Optional[str] = None,
        timeout_override: Optional[int] = None,
    ) -> str:
        """Send a chat completion with a caller-supplied system prompt.

        Used by F013 IntentRouter and ChatLLMService where the persona
        is not the test-plan assistant.

        Args:
            system_prompt: System instruction (non-empty).
            user_content: User-turn text (non-empty).
            images: Optional image file paths (base64-encoded).
            model_override: If provided, override the configured model name.
            timeout_override: If provided, override the configured timeout.

        Returns:
            The model's text response.

        Raises:
            LLMClientError: On any failure (timeout, connection, API status,
                or unexpected error) or when the LLM configuration is
                incomplete (missing API URL / API key / model name).
        """
        if not system_prompt or not system_prompt.strip():
            raise LLMClientError("system_prompt 不能为空")
        if not user_content or not user_content.strip():
            raise LLMClientError("user_content 不能为空")

        user_message = await self._build_user_message(user_content, images)
        return await self._do_chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
            model_override=model_override,
            timeout_override=timeout_override,
        )

    async def stream_with_system(
        self,
        system_prompt: str,
        user_content: str,
        images: Optional[List[str]] = None,
        *,
        model_override: Optional[str] = None,
        timeout_override: Optional[int] = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> AsyncIterator[str]:
        """Stream a caller-supplied chat completion as text chunks."""
        if not system_prompt or not system_prompt.strip():
            raise LLMClientError("system_prompt 不能为空")
        if not user_content or not user_content.strip():
            raise LLMClientError("user_content 不能为空")

        cfg = self._resolve_config()
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise LLMClientError(
                "缺少 openai 依赖，请先安装 requirements.txt 中的 openai。"
            ) from exc

        base_url = self._normalize_base_url(cfg.api_url)
        api_key = self._resolve_api_key(cfg)
        model_name = (model_override or cfg.model_name).strip()
        # 2026-07：流式路径同样使用统一解析函数；调用级/用户/Profile/系统默认
        # 优先级一致，参考 _resolve_effective_timeout 文档。
        timeout, timeout_debug = _resolve_effective_timeout(
            cfg, system_prompt, timeout_override
        )
        # 2026-07：流式路径同样需要把 max_tokens 传出去。
        max_tokens = _resolve_max_tokens(cfg, system_prompt)
        # 2026-07：与 max_tokens 对称。``TEST_PLAN_PROFILE.temperature=0.2``
        # 也是死配置；不显式下发会让 provider 用默认（通常 1.0），
        # JSON 结构化输出的稳定性塌方。
        temperature = _resolve_temperature(cfg, system_prompt)

        if not base_url:
            raise LLMClientError("模型 API 地址不能为空，请先在'设置'页面配置")
        if not api_key:
            raise LLMClientError(
                "模型 API Key 不能为空，请先在'设置'页面配置"
            )
        if not model_name:
            raise LLMClientError("模型名称不能为空，请先在'设置'页面配置")

        user_message = await self._build_user_message(user_content, images)
        # The streaming request-start event records the textual user input
        # length.  Keep it independent of the multimodal payload structure
        # (which can be a list when images are present).
        prompt_length = len(user_content)
        await self._rate_limiter.acquire()
        start_time = time.monotonic()
        log_event(
            logging.getLogger("testagent.integration"),
            logging.INFO,
            LogEvent.LLM_REQUEST_STARTED,
            "LLM request started",
            provider=base_url,
            model=model_name,
            input_tokens=None,
            input_length=prompt_length,
        )
        try:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,
            )
            from app.core.observability import start_span
            with start_span(
                "llm.request",
                {"gen_ai.provider": base_url, "gen_ai.model": model_name, "gen_ai.operation": "chat"},
            ):
                stream = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        user_message,
                    ],
                    extra_body={"enable_thinking": getattr(cfg, "enable_thinking", False)},
                    stream=True,
                    stream_options={"include_usage": True},
                    **({"max_tokens": max_tokens} if max_tokens else {}),
                    **({"temperature": temperature} if temperature is not None else {}),
                )
            emitted = False
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                completion_tokens = getattr(usage, "completion_tokens", None)
                if on_usage is not None and (
                    prompt_tokens is not None or completion_tokens is not None
                ):
                    on_usage({
                        "input": int(prompt_tokens or 0),
                        "output": int(completion_tokens or 0),
                    })
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None)
                if not content:
                    continue
                emitted = True
                yield self._stringify_content(content)
            elapsed = time.monotonic() - start_time
            if not emitted:
                logger.error(
                    "主模型流式返回空内容 | 模型: %s | 耗时: %.1fs",
                    model_name,
                    elapsed,
                )
                raise LLMClientError("模型响应中未找到可用内容")
        except self._import_api_errors()["APITimeoutError"] as exc:
            raise LLMClientError(f"LLM streaming timeout: {exc}") from exc
        except self._import_api_errors()["APIConnectionError"] as exc:
            raise LLMClientError(f"LLM streaming connection failed: {exc}") from exc
        except self._import_api_errors()["APIStatusError"] as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", "?")
            response_body = getattr(getattr(exc, "response", None), "text", "") or str(exc)
            raise LLMClientError(
                f"LLM streaming API error (HTTP {status_code}): {response_body}"
            ) from exc
        except LLMClientError:
            raise
        except Exception as exc:
            raise LLMClientError(f"LLM streaming failed: {exc}") from exc

    async def continue_generation(
        self, partial_response: str, original_prompt: str
    ) -> str:
        """Continue a truncated JSON response.

        Sends the tail of the partial JSON (last 2000 chars) along with
        a continuation system prompt, asking the model to complete the
        JSON from where it was cut off.

        Args:
            partial_response: The incomplete JSON generated so far.
            original_prompt: The original prompt (for context, not sent).

        Returns:
            Continuation text that should be appended to ``partial_response``.
        """
        cfg = self._resolve_config()

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise LLMClientError(
                "缺少 openai 依赖，请先安装 requirements.txt 中的 openai。"
            ) from exc

        base_url = self._normalize_base_url(cfg.api_url)
        api_key = self._resolve_api_key(cfg)
        model_name = cfg.model_name.strip()
        # 2026-07：续写路径同样使用统一解析函数
        timeout, timeout_debug = _resolve_effective_timeout(
            cfg, system_prompt="", request_override=None
        )

        logger.info(
            "JSON 续写调用开始 | 模型: %s | user_configured_timeout=%s | "
            "effective_timeout=%ds | timeout_source=%s | 已有内容长度: %d 字符",
            model_name,
            timeout_debug["user_configured_timeout"],
            timeout_debug["effective_timeout"],
            timeout_debug["timeout_source"],
            len(partial_response),
        )
        await self._rate_limiter.acquire()
        start_time = time.monotonic()

        try:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,
            )
            completion = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一名严谨的项目测试方案生成助手。"
                            "你之前生成的 JSON 响应因输出长度限制被截断，现在需要你继续完成。"
                            "请直接从截断处继续输出剩余的 JSON 内容，不要重复已有的部分，不要输出任何解释文字。"
                            "确保最终输出是一个完整闭合的 JSON 对象。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "以下是你之前生成的被截断的 JSON（已省略前面完整部分，只保留截断位置附近的内容）：\n\n"
                            f"```json\n{partial_response[-2000:]}\n```\n\n"
                            "请从截断处（即上面最后一行之后）继续输出剩余的 JSON 内容，"
                            "确保所有括号和引号正确闭合，最终形成完整的 JSON 对象。"
                            "只输出需要补充的部分，不要重复已有内容。"
                        ),
                    },
                ],
                extra_body={"enable_thinking": getattr(cfg, "enable_thinking", False)},
                stream=False,
            )
            continuation = self._extract_completion_content(completion).strip()
            elapsed = time.monotonic() - start_time

            if not continuation:
                logger.error("JSON 续写返回空内容 | 耗时: %.1fs", elapsed)
                raise LLMClientError("续写响应为空")

            logger.info(
                "JSON 续写调用成功 | 耗时: %.1fs | 续写长度: %d 字符",
                elapsed,
                len(continuation),
            )
            return continuation

        except self._import_api_errors()["APITimeoutError"] as exc:
            elapsed = time.monotonic() - start_time
            logger.error("JSON 续写调用超时 | 耗时: %.1fs | 错误: %s", elapsed, exc)
            raise LLMClientError(f"续写调用超时：{exc}") from exc

        except self._import_api_errors()["APIConnectionError"] as exc:
            elapsed = time.monotonic() - start_time
            logger.error("JSON 续写连接失败 | 耗时: %.1fs | 错误: %s", elapsed, exc)
            raise LLMClientError(f"续写连接失败：{exc}") from exc

        except self._import_api_errors()["APIStatusError"] as exc:
            elapsed = time.monotonic() - start_time
            logger.error("JSON 续写接口错误 | 耗时: %.1fs | 错误: %s", elapsed, exc)
            raise LLMClientError(f"续写接口错误：{exc}") from exc

        except LLMClientError:
            raise

        except Exception as exc:
            elapsed = time.monotonic() - start_time
            logger.error("JSON 续写未知异常 | 耗时: %.1fs | 错误: %s", elapsed, exc)
            raise LLMClientError(f"续写调用失败：{exc}") from exc

    async def health_check(self) -> bool:
        """Test connectivity to the configured model API with a lightweight request."""
        # Reset the previous error so a fresh attempt starts clean
        self._last_health_error = None
        cfg = self._resolve_config()

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            self._last_health_error = f"ImportError: {exc}"
            return False

        base_url = self._normalize_base_url(cfg.api_url)
        api_key = self._resolve_api_key(cfg)
        model_name = cfg.model_name.strip()
        # 2026-07：health_check 也走统一解析，但强制 30s 上限，避免
        # 用户配 300s 时点测试按钮要等 5 分钟。Profile default 不适用
        # (system_prompt 为空)，走 user → system_default 路径。
        timeout, _ = _resolve_effective_timeout(
            cfg, system_prompt="", request_override=None
        )
        timeout = min(timeout, 30)

        if not base_url or not api_key or not model_name:
            missing = [
                name
                for name, val in (("api_url", base_url), ("api_key", api_key), ("model_name", model_name))
                if not val
            ]
            self._last_health_error = f"missing_config: {','.join(missing)}"
            logger.warning("LLM health_check skipped: missing config (%s)", self._last_health_error)
            return False

        try:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
            completion = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "user",
                        "content": "请回复一个简短的确认：连通性测试通过。",
                    }
                ],
                max_tokens=32,
                stream=False,
            )
            content = ""
            if completion.choices:
                content = completion.choices[0].message.content or ""
            logger.info("LLM health_check 成功 | 响应: %s", content.strip()[:100])
            return True
        except Exception as exc:
            logger.warning("LLM health_check 失败: %s", exc)
            # Stash the error class on the client for callers to surface
            self._last_health_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            return False

    # ── Content extraction (four formats) ──────────────────────────

    @staticmethod
    def _extract_completion_content(completion: object) -> str:
        """Extract text content from a completion object.

        Handles:
        - Standard ``choices[0].message.content``
        - Dict-based responses
        - Stream chunk aggregation
        """
        choices = getattr(completion, "choices", None)
        if choices:
            choice = choices[0]
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None)
            if content:
                return LLMClient._stringify_content(content)
            text = getattr(choice, "text", None)
            if text:
                return str(text)

        if isinstance(completion, dict):
            return LLMClient._extract_content(completion)

        # Stream chunks
        answer_parts = []
        try:
            iterator = iter(completion)
        except TypeError:
            iterator = iter(())
        for chunk in iterator:
            chunk_choices = getattr(chunk, "choices", None)
            if not chunk_choices:
                continue
            delta = getattr(chunk_choices[0], "delta", None)
            content = getattr(delta, "content", None)
            if content:
                answer_parts.append(LLMClient._stringify_content(content))
        return "".join(answer_parts)

    @staticmethod
    def _stringify_content(content: object) -> str:
        """Convert a content value (str, list, or object) to a single string."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                else:
                    text = getattr(item, "text", None) or getattr(
                        item, "content", None
                    )
                if text:
                    parts.append(str(text))
            return "".join(parts)
        return str(content)

    @staticmethod
    def _completion_finish_reason(completion: object) -> str:
        """Extract ``finish_reason`` from a completion object."""
        choices = getattr(completion, "choices", None)
        if choices:
            return str(getattr(choices[0], "finish_reason", "") or "")
        if isinstance(completion, dict):
            choices = completion.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    return str(first.get("finish_reason") or "")
        return ""

    @staticmethod
    def _extract_content(data: Dict[str, object]) -> str:
        """Extract content from a dict-shaped completion."""
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and message.get("content"):
                    return str(message["content"])
                if first.get("text"):
                    return str(first["text"])
        if data.get("content"):
            return str(data["content"])
        raise LLMClientError("模型响应中未找到可用内容")

    # ── Message construction ───────────────────────────────────────

    async def _build_user_message(
        self, prompt: str, images: Optional[List[str]] = None
    ) -> Dict[str, object]:
        """Build the user message dict, optionally attaching images as base64 data URLs.

        When images are provided, the message uses the multi-part content
        format (text + image_url blocks).
        """
        if not images:
            return {"role": "user", "content": prompt}

        content: list = [{"type": "text", "text": prompt}]
        for image_path in images:
            try:
                # Run blocking I/O in a thread to avoid blocking the event loop
                image_data = await asyncio.to_thread(
                    _encode_image_base64, image_path
                )
                ext = image_path.rsplit(".", 1)[-1].lower() if "." in image_path else "png"
                mime = (
                    f"image/{ext}"
                    if ext in ("png", "jpeg", "jpg", "gif", "webp")
                    else "image/png"
                )
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_data}"},
                    }
                )
            except Exception as exc:
                logger.warning("主模型图片编码失败: %s | 错误: %s", image_path, exc)
        return {"role": "user", "content": content}

    # ── URL normalisation ──────────────────────────────────────────

    @staticmethod
    def _normalize_base_url(api_url: str) -> str:
        """Strip trailing slashes and ``/chat/completions`` suffix."""
        url = api_url.strip().rstrip("/")
        chat_suffix = "/chat/completions"
        if url.endswith(chat_suffix):
            return url[: -len(chat_suffix)]
        return url

    @staticmethod
    def _normalize_chat_url(api_url: str) -> str:
        """Ensure the URL ends with the appropriate chat completions path."""
        url = api_url.strip().rstrip("/")
        if not url:
            return ""
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/compatible-mode/v1") or url.endswith("/v1"):
            return f"{url}/chat/completions"
        return url

    # ── Config resolution ─────────────────────────────────────────

    def _resolve_config(self) -> Any:
        """Resolve effective configuration from the config provider.

        F015 — the system-wide ``_EnvConfig`` fallback has been
        removed.  When ``build_llm_config_provider`` cannot find a
        config for the current user it returns an
        ``LLMNotConfiguredMarker`` (with ``is_unconfigured=True``);
        that marker is propagated through here and detected by
        ``_do_chat_completion`` which raises a clear
        ``LLMClientError("LLM_NOT_CONFIGURED: ...")`` so the calling
        tool surfaces a friendly error to the frontend.
        """
        if self._config_provider is None:
            logger.warning("LLMClient._resolve_config: 无 config_provider")
            raise LLMClientError(
                "LLM_NOT_CONFIGURED: 当前用户未配置模型，请前往'设置'页面配置 API 地址 / Key / 模型名"
            )
        return self._config_provider

    # ── Shared chat-completion implementation ─────────────────────

    async def _do_chat_completion(
        self,
        system_prompt: str,
        user_message: Dict[str, object],
        *,
        model_override: Optional[str] = None,
        timeout_override: Optional[int] = None,
    ) -> str:
        """Internal chat-completion core shared by ``generate`` and
        ``generate_with_system``.

        Handles:
        - Lazy import of ``openai.AsyncOpenAI``
        - URL normalisation, API key resolution, model/timeout resolution
        - Rate-limit acquisition
        - OpenAI chat completion call
        - Four-category error classification (timeout / connection /
          status / unknown) with re-raise as ``LLMClientError``
        - Content extraction and empty-response detection

        Callers must supply a built ``user_message`` dict (use
        ``_build_user_message`` for plain text + optional images).
        """
        cfg = self._resolve_config()

        # F015: detect LLMNotConfiguredMarker early so we never even
        # attempt to talk to the upstream API.  Tool callers see a
        # clean LLMClientError that maps to the "未配置" UX.
        if getattr(cfg, "is_unconfigured", False):
            logger.warning(
                "LLMClient._do_chat_completion: 检测到 LLM_NOT_CONFIGURED, 即将抛出友好错误"
            )
            raise LLMClientError(
                "LLM_NOT_CONFIGURED: 当前用户未配置模型，请前往'设置'页面配置 API 地址 / Key / 模型名"
            )

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise LLMClientError(
                "缺少 openai 依赖，请先安装 requirements.txt 中的 openai。"
            ) from exc

        base_url = self._normalize_base_url(cfg.api_url)
        api_key = self._resolve_api_key(cfg)
        model_name = (model_override or cfg.model_name).strip()
        # 2026-07：超时解析。完整优先级：调用级 override > 用户模型配置 >
        # Profile 协议级 default > 系统默认。上限 300s。日志字段在下方打印。
        timeout, timeout_debug = _resolve_effective_timeout(
            cfg, system_prompt, timeout_override
        )

        # 2026-07：把 max_tokens 真正传出去。``TEST_PLAN_PROFILE.max_tokens``
        # 是死配置（TestPlanGeneratorTool 走 ``generate()``，profile 不会被读），
        # 这里用 system-prompt 启发式 + ``cfg.max_tokens`` 二级解析，避免
        # 依赖 provider 默认（默认 4096 会截断 23k 字符的中文响应）。
        max_tokens = _resolve_max_tokens(cfg, system_prompt)
        # 2026-07：与 max_tokens 对称。``TEST_PLAN_PROFILE.temperature=0.2``
        # 也是死配置；不显式下发会让 provider 用默认（通常 1.0），
        # JSON 结构化输出的稳定性塌方。
        temperature = _resolve_temperature(cfg, system_prompt)

        if not base_url:
            raise LLMClientError("模型 API 地址不能为空")
        if not api_key:
            raise LLMClientError(
                "模型 API Key 不能为空，请先在'设置'页面配置"
            )
        if not model_name:
            raise LLMClientError("模型名称不能为空，请先在'设置'页面配置")

        logger.debug(
            "LLMClient._do_chat_completion: 准备调用 | base=%s | model=%s | timeout=%ds",
            (base_url or "")[:40], model_name, timeout,
        )

        # Best-effort log of the user prompt for debugging. We don't log
        # the system prompt here to avoid duplicate dumps in tools that
        # also call ``dump_prompt`` themselves.
        user_text = user_message.get("content") if isinstance(user_message, dict) else None
        prompt_length = len(user_text) if isinstance(user_text, str) else 0
        logger.info(
            "主模型调用开始 | 模型: %s | API地址: %s | user_configured_timeout=%s | "
            "profile_default_timeout=%s | request_override_timeout=%s | "
            "effective_timeout=%ds | timeout_source=%s | cache_hit=%s | 提示词长度: %d 字符",
            model_name,
            base_url,
            timeout_debug["user_configured_timeout"],
            timeout_debug["profile_default_timeout"],
            timeout_debug["request_override_timeout"],
            timeout_debug["effective_timeout"],
            timeout_debug["timeout_source"],
            getattr(self, "_last_cache_hit", None),
            prompt_length,
        )
        await self._rate_limiter.acquire()
        start_time = time.monotonic()

        try:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,
            )
            completion = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    user_message,
                ],
                extra_body={"enable_thinking": getattr(cfg, "enable_thinking", False)},
                stream=False,
                **({"max_tokens": max_tokens} if max_tokens else {}),
                **({"temperature": temperature} if temperature is not None else {}),
            )
            answer = self._extract_completion_content(completion).strip()
            elapsed = time.monotonic() - start_time
            if not answer:
                logger.error(
                    "主模型返回空内容 | 模型: %s | API地址: %s | 耗时: %.1fs",
                    model_name,
                    base_url,
                    elapsed,
                )
                raise LLMClientError("模型响应中未找到可用内容")

            logger.info(
                "主模型调用成功 | 模型: %s | 耗时: %.1fs | 响应长度: %d 字符",
                model_name,
                elapsed,
                len(answer),
            )
            log_event(
                logging.getLogger("testagent.integration"),
                logging.INFO,
                LogEvent.LLM_REQUEST_COMPLETED,
                "LLM request completed",
                provider=base_url,
                model=model_name,
                duration_ms=round(elapsed * 1000, 2),
                output_length=len(answer),
            )
            return answer

        except self._import_api_errors()["APITimeoutError"] as exc:
            elapsed = time.monotonic() - start_time
            logger.error(
                "主模型调用超时 | 模型: %s | API地址: %s | 超时配置: %ds | 已等待: %.1fs | 原始错误: %s",
                model_name,
                base_url,
                timeout,
                elapsed,
                exc,
            )
            raise LLMClientError(
                f"模型调用超时（配置超时 {timeout}s，已等待 {elapsed:.0f}s），"
                f"请适当调大超时时间或检查网络：{exc}"
            ) from exc

        except self._import_api_errors()["APIConnectionError"] as exc:
            elapsed = time.monotonic() - start_time
            logger.error(
                "主模型连接失败 | 模型: %s | API地址: %s | 耗时: %.1fs | 原始错误: %s",
                model_name,
                base_url,
                elapsed,
                exc,
            )
            raise LLMClientError(
                f"模型连接失败，请检查网络、API 地址和代理设置：{exc}"
            ) from exc

        except self._import_api_errors()["APIStatusError"] as exc:
            elapsed = time.monotonic() - start_time
            status_code = getattr(getattr(exc, "response", None), "status_code", "?")
            response_body = ""
            try:
                response_body = getattr(exc.response, "text", "") or str(exc)
            except Exception:
                response_body = str(exc)
            if len(response_body) > 500:
                response_body = response_body[:500] + "..."
            logger.error(
                "主模型接口返回错误 | 模型: %s | API地址: %s | HTTP状态码: %s | 耗时: %.1fs | 响应内容: %s",
                model_name,
                base_url,
                status_code,
                elapsed,
                response_body,
            )
            raise LLMClientError(
                f"模型接口返回错误（HTTP {status_code}）：{response_body}"
            ) from exc

        except LLMClientError as exc:
            log_event(
                logging.getLogger("testagent.integration"),
                logging.ERROR,
                LogEvent.LLM_REQUEST_FAILED,
                "LLM request failed",
                provider=base_url,
                model=model_name,
                duration_ms=round((time.monotonic() - start_time) * 1000, 2),
                error_type=type(exc).__name__,
            )
            raise

        except Exception as exc:
            elapsed = time.monotonic() - start_time
            logger.error(
                "主模型调用未知异常 | 模型: %s | API地址: %s | 耗时: %.1fs | 异常类型: %s | 原始错误: %s",
                model_name,
                base_url,
                elapsed,
                type(exc).__name__,
                exc,
            )
            raise LLMClientError(f"模型调用失败：{exc}") from exc

    @staticmethod
    def _resolve_api_key(cfg: Any) -> str:
        """Resolve the API key, supporting a ``get_effective_api_key()`` method."""
        if hasattr(cfg, "get_effective_api_key"):
            return cfg.get_effective_api_key()
        if hasattr(cfg, "api_key"):
            return cfg.api_key or ""
        return ""

    # ── Error helpers ──────────────────────────────────────────────

    @staticmethod
    def _import_api_errors() -> dict:
        """Import and return the openai error classes (cached)."""
        try:
            from openai import APIConnectionError, APIStatusError, APITimeoutError
        except ImportError:
            # If openai isn't installed, return dummy types so isinstance checks fail gracefully
            return {
                "APITimeoutError": type("APITimeoutError", (Exception,), {}),
                "APIConnectionError": type("APIConnectionError", (Exception,), {}),
                "APIStatusError": type("APIStatusError", (Exception,), {}),
            }
        return {
            "APITimeoutError": APITimeoutError,
            "APIConnectionError": APIConnectionError,
            "APIStatusError": APIStatusError,
        }

    # ── Task-contract wrapper (F014) ───────────────────────────────

    async def generate_with_profile(
        self,
        profile: Any,                       # LLMTaskProfile (avoid import cycle)
        user_content: str,
        *,
        images: Optional[List[str]] = None,
        parser: Any = None,                  # ParserAdapter (overrides registry)
        system_prompt_override: Optional[str] = None,
    ) -> "LLMProfileResult":
        """Send an LLM call and parse the response per the given profile.

        This is a thin wrapper around :meth:`generate_with_system`.  It
        does **not** introduce a new model client, does not read any
        new configuration source, and does not duplicate any prompt
        construction logic.

        Flow:

          1. Call ``generate_with_system(effective_system_prompt,
             user_content, images=images, timeout_override=...)``, where
             ``effective_system_prompt = system_prompt_override`` when the
             override is provided (even an empty string), else
             ``profile.system_prompt``.
          2. On LLM transport error → return ``LLMProfileResult(
             success=False, error_type='llm_error', ...)``; the caller
             decides what to do (chat → fallback text, intent → CLARIFY).
          3. On a successful raw response, run the profile-selected
             ``ParserAdapter`` (``parser`` arg overrides the registry).
          4. On parse failure, honour ``profile.on_parse_failure``:
             - ``FALLBACK_DEFAULT`` → return ``success=False``,
               ``parsed=profile.fallback_text`` (parsed by the same
               adapter as a second pass when ``fallback_text`` is a
               JSON string and the parser is JSON).
             - ``RAISE``           → raise ``LLMProfileParseError``.
             - ``RETRY``           → currently treated as ``RAISE``
               (the batch did not implement cross-call retry).

        Args:
            profile: An ``LLMTaskProfile`` instance.
            user_content: The user-turn text.
            images: Optional image file paths (base64-encoded).
            parser: Optional pre-built ``ParserAdapter``.  If omitted,
                the profile's parser type is resolved through
                ``app.llm.parsers.registry.get_parser``.
            system_prompt_override: When provided (even an empty string),
                used as the system prompt *in place of*
                ``profile.system_prompt``.  ``None`` (the default) means
                "use the profile default".  An explicit empty string is
                intentionally NOT silently replaced by the profile default —
                it falls through to the existing non-empty check and raises
                ``LLMClientError("system_prompt 不能为空")``.  The profile
                object is never mutated; runtime prompts never leak between
                concurrent callers.

        Returns:
            ``LLMProfileResult`` with ``success``, ``raw_text``,
            ``parsed``, ``error_type``, ``error_message``.
        """
        # Local imports avoid an import cycle (llm package imports
        # from llm_client in its adapters / registry).
        from app.llm.errors import LLMProfileError, LLMProfileParseError
        from app.llm.parsers.registry import get_parser

        if profile is None:
            raise LLMProfileError("generate_with_profile: profile is required")

        # 动态 Prompt 合同 (Phase 2.9B): 显式 override 严格优先于 Profile 默认。
        # None → 用 Profile 默认;传了(含空串)→ 严格用传入值,空串继续由
        # generate_with_system 的非空校验抛错,绝不静默 fallback 到 Profile。
        effective_system_prompt = (
            profile.system_prompt
            if system_prompt_override is None
            else system_prompt_override
        )
        logger.info(
            "LLMClient.generate_with_profile: 开始 | profile=%s | parser=%s | "
            "system_prompt_source=%s | system_prompt_len=%d",
            profile.name,
            profile.parser.value,
            "override" if system_prompt_override is not None else "profile",
            len(effective_system_prompt or ""),
        )

        # 1) LLM call (re-uses existing generate_with_system path).
        try:
            raw_text = await self.generate_with_system(
                effective_system_prompt,
                user_content,
                images=images,
                timeout_override=profile.timeout_override,
            )
        except LLMClientError as exc:
            logger.warning(
                "LLMClient.generate_with_profile: LLM 调用失败 | profile=%s | err=%s",
                profile.name, exc,
            )
            return self._handle_llm_failure(profile, raw_text="", exc=exc)
        except Exception as exc:  # noqa: BLE001 — translate to result, not raise
            logger.warning(
                "LLMClient.generate_with_profile: LLM 异常 | profile=%s | exc=%s",
                profile.name, type(exc).__name__,
            )
            return self._handle_llm_failure(
                profile, raw_text="", exc=exc, error_type="llm_unexpected"
            )

        # 2) Parse.
        adapter = parser or get_parser(profile.parser)
        logger.info(
            "LLMClient.generate_with_profile: 解析开始 | profile=%s | parser=%s | 原始长度=%d",
            profile.name, type(adapter).__name__, len(raw_text),
        )
        try:
            parsed = adapter.parse(raw_text, profile)
        except LLMProfileParseError as exc:
            logger.warning(
                "LLMClient.generate_with_profile: 解析失败 | profile=%s | 原始长度=%d | err=%s",
                profile.name, len(raw_text), str(exc)[:200],
            )
            return self._handle_parse_failure(
                profile, raw_text, exc, adapter
            )

        return LLMProfileResult(
            task_name=profile.name,
            raw_text=raw_text,
            parsed=parsed,
            success=True,
            error_type=None,
            error_message=None,
        )

    async def stream_with_profile(
        self,
        profile: Any,
        user_content: str,
        *,
        images: Optional[List[str]] = None,
        system_prompt_override: Optional[str] = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> AsyncIterator[str]:
        """Stream a profile-backed chat completion as raw text chunks."""
        from app.llm.errors import LLMProfileError

        if profile is None:
            raise LLMProfileError("stream_with_profile: profile is required")
        async for chunk in self.stream_with_system(
            profile.system_prompt if system_prompt_override is None else system_prompt_override,
            user_content,
            images=images,
            timeout_override=profile.timeout_override,
            on_usage=on_usage,
        ):
            yield chunk

    @staticmethod
    def _handle_parse_failure(
        profile: Any,
        raw_text: str,
        exc: Exception,
        adapter: Any,
    ) -> "LLMProfileResult":
        """Apply ``on_parse_failure`` policy and produce a result."""
        from app.llm.errors import LLMProfileConfigError, LLMProfileParseError
        from app.llm.task_profiles import LLMParseFailurePolicy

        policy = profile.on_parse_failure
        if policy == LLMParseFailurePolicy.RAISE:
            raise LLMProfileParseError(
                f"{profile.name}: parse failed and on_parse_failure=RAISE: {exc}"
            ) from exc
        if policy == LLMParseFailurePolicy.RETRY:
            # Reserved for future cross-call retry.  For now treat as
            # RAISE so the caller cannot silently swallow the failure.
            raise LLMProfileParseError(
                f"{profile.name}: parse failed and on_parse_failure=RETRY "
                f"(retry not implemented in F014): {exc}"
            ) from exc

        # FALLBACK_DEFAULT — try to parse the fallback_text through the
        # same adapter so the caller always receives a typed value.
        fallback_text = profile.fallback_text
        if fallback_text is None:
            return LLMProfileResult(
                task_name=profile.name,
                raw_text=raw_text,
                parsed=None,
                success=False,
                error_type="parse_error_no_fallback",
                error_message=str(exc),
            )
        try:
            parsed_fallback = adapter.parse(fallback_text, profile)
        except LLMProfileParseError as inner:
            return LLMProfileResult(
                task_name=profile.name,
                raw_text=raw_text,
                parsed=fallback_text,  # surface raw fallback as a string
                success=False,
                error_type="parse_error",
                error_message=f"{exc}; fallback also failed: {inner}",
            )
        return LLMProfileResult(
            task_name=profile.name,
            raw_text=raw_text,
            parsed=parsed_fallback,
            success=False,
            error_type="parse_error",
            error_message=str(exc),
        )

    @staticmethod
    def _handle_llm_failure(
        profile: Any,
        *,
        raw_text: str,
        exc: BaseException,
        error_type: str = "llm_error",
    ) -> "LLMProfileResult":
        """Translate an LLM transport failure into a profile-shaped result.

        If the profile has a ``fallback_text``, surface it as ``parsed``
        so callers (e.g. ChatLLMService) can render it directly.  For
        strict-JSON contracts the fallback string is parsed once via
        the JSON adapter so the caller still receives a typed dict.
        """
        from app.llm.parsers.registry import get_parser
        from app.llm.task_profiles import LLMParseFailurePolicy

        fallback_text = profile.fallback_text
        if profile.on_parse_failure == LLMParseFailurePolicy.RAISE:
            # RAISE means the caller wants no silent fallback — but for
            # LLM transport errors the only sane default is to surface
            # the failure.  We still attach the fallback_text as the
            # ``parsed`` value so any code that survives an LLM
            # outage can render something user-facing.
            return LLMProfileResult(
                task_name=profile.name,
                raw_text=raw_text,
                parsed=fallback_text,
                success=False,
                error_type=error_type,
                error_message=f"{type(exc).__name__}: {exc}",
            )

        if fallback_text is None:
            return LLMProfileResult(
                task_name=profile.name,
                raw_text=raw_text,
                parsed=None,
                success=False,
                error_type=error_type,
                error_message=f"{type(exc).__name__}: {exc}",
            )

        try:
            adapter = get_parser(profile.parser)
            parsed_fallback = adapter.parse(fallback_text, profile)
        except Exception:  # noqa: BLE001 — best-effort
            parsed_fallback = fallback_text
        return LLMProfileResult(
            task_name=profile.name,
            raw_text=raw_text,
            parsed=parsed_fallback,
            success=False,
            error_type=error_type,
            error_message=f"{type(exc).__name__}: {exc}",
        )


# ── Profile result (F014) ──────────────────────────────────────────


class LLMProfileResult:
    """Return value of ``LLMClient.generate_with_profile``.

    Attributes:
        task_name: Profile name (mirrors ``LLMTaskProfile.name``).
        raw_text: The raw LLM response text.  Always set, even on
            failure, so the caller can log / dump it for debugging.
        parsed: Parsed value (``str`` or ``dict``) on success, or the
            ``fallback_text`` re-parsed on a parse-failure fallback.
            ``None`` only when fallback was unavailable.
        success: True when the LLM call AND parse succeeded.
        error_type: One of ``None`` / ``'llm_error'`` / ``'llm_unexpected'``
            / ``'parse_error'`` / ``'parse_error_no_fallback'``.
        error_message: Human-readable diagnostic; never contains the
            API key (only the public error class + brief message).
    """

    __slots__ = (
        "task_name",
        "raw_text",
        "parsed",
        "success",
        "error_type",
        "error_message",
    )

    def __init__(
        self,
        *,
        task_name: str,
        raw_text: str,
        parsed: Any,
        success: bool,
        error_type: Optional[str],
        error_message: Optional[str],
    ) -> None:
        self.task_name = task_name
        self.raw_text = raw_text
        self.parsed = parsed
        self.success = success
        self.error_type = error_type
        self.error_message = error_message

    def __repr__(self) -> str:
        return (
            f"LLMProfileResult(task={self.task_name!r}, "
            f"success={self.success}, error_type={self.error_type!r})"
        )


# ── Mock LLM client (for testing) ─────────────────────────────────


class MockLLMClient:
    """Mock client that returns structured stub responses.

    Preserved from legacy for tests and development without a real API key.
    """

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or "MockLLM-PlanWise"

    async def generate(self, prompt: str, images: Optional[List[str]] = None) -> str:
        logger.info("MockLLM 生成开始 | 模型: %s", self.model_name)
        await asyncio.sleep(1.2)
        project_hint = self._extract_project_hint(prompt)
        logger.info("MockLLM 生成完成 | 项目: %s", project_hint)
        return f"""# {project_hint}测试方案

## 项目概述
本文档为"{project_hint}"的项目测试方案，基于需求文档与标准测试方案模板生成，覆盖功能、非功能、接口、兼容性和交付质量要求。

## 测试目标
1. 验证核心业务流程符合需求描述。
2. 验证异常场景、边界条件和数据一致性。
3. 评估系统性能、稳定性、兼容性和安全性。
4. 形成可复用的测试交付物，支撑上线评审。

## 测试范围
| 范围类型 | 包含内容 | 不包含内容 |
| --- | --- | --- |
| 功能范围 | 需求文档中声明的核心功能、流程和权限 | 未确认或延期需求 |
| 非功能范围 | 性能、兼容性、安全性、易用性 | 第三方平台内部实现 |
| 平台范围 | Web、桌面端或移动端适配范围 | 未纳入项目计划的平台 |

## 测试策略
采用需求评审、测试设计、功能测试、接口测试、回归测试、兼容性测试和验收测试相结合的策略。高风险模块优先覆盖，核心链路执行正向、逆向和边界用例。

## 测试环境
| 环境 | 配置 |
| --- | --- |
| 测试环境 | 独立测试服务器、测试数据库、测试账号 |
| 浏览器 | Chrome、Edge 最新稳定版 |
| 数据 | 脱敏业务数据与专项构造数据 |

## 测试资源
| 角色 | 职责 |
| --- | --- |
| 测试负责人 | 制定计划、跟踪风险、组织评审 |
| 测试工程师 | 编写用例、执行测试、提交缺陷 |
| 开发工程师 | 修复缺陷、配合定位问题 |

## 测试进度
| 阶段 | 工作内容 | 产出 |
| --- | --- | --- |
| 测试准备 | 需求分析、测试计划、环境准备 | 测试方案、测试用例 |
| 测试执行 | 功能测试、接口测试、缺陷回归 | 缺陷记录、执行报告 |
| 测试总结 | 风险复盘、上线评审 | 测试报告 |

## 准入准出标准
准入标准：需求已评审通过，测试环境可用，核心测试数据已准备，版本构建可部署。

准出标准：阻塞和严重缺陷已关闭，核心用例执行通过，遗留风险已确认，测试报告完成评审。

## 风险分析
| 风险 | 影响 | 应对措施 |
| --- | --- | --- |
| 需求变更频繁 | 影响测试范围和进度 | 建立变更同步机制 |
| 环境不稳定 | 影响测试执行效率 | 提前准备备用环境 |
| 模板结构差异 | 影响文档一致性 | 导出前进行章节完整性校验 |

## 测试交付物
1. 测试方案
2. 测试用例
3. 缺陷清单
4. 测试执行记录
5. 测试总结报告
"""

    async def health_check(self) -> bool:
        return True

    @staticmethod
    def _extract_project_hint(prompt: str) -> str:
        for keyword in ("系统", "平台", "工具", "项目"):
            index = prompt.find(keyword)
            if index > 0:
                start = max(0, index - 12)
                return (
                    prompt[start : index + len(keyword)]
                    .replace("\n", "")
                    .strip(" ：:，,。.")
                )
        return "项目"


# ── Module helpers ────────────────────────────────────────────────


def _encode_image_base64(image_path: str) -> str:
    """Read an image file and return its base64-encoded string (sync I/O)."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ── F015: Legacy _EnvConfig removed ────────────────────────────────
#
# The ``_EnvConfig`` class used to provide a final fallback when no
# config provider was set.  F015 deletes this fallback: model
# configuration is now per-user and DB-only.  When the user has no row
# in ``model_configs``, ``SettingsService.build_llm_config_provider``
# returns an ``LLMNotConfiguredMarker`` (with ``is_unconfigured=True``)
# and ``LLMClient._do_chat_completion`` raises a clear
# ``LLMClientError("LLM_NOT_CONFIGURED: ...")``.  The frontend
# detects this and routes the user to the Settings page.
