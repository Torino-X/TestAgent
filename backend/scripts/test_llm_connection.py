#!/usr/bin/env python3
"""Minimum LLM connectivity test.

Design rules:
  - No hardcoded api_url, model_name, or api_key.
  - Full API key NEVER printed; only first 4 + last 4 chars.
  - Missing key/model aborts with clear message -- NO silent MockLLMClient.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Ensure backend on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

# Load .env
from app.core.config import get_settings as _get_settings  # noqa: E402
_settings = _get_settings()

_ENV_FILE = _BACKEND_DIR / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("llm_connectivity_test")


def mask(s, keep=4):
    if len(s) <= keep * 2 + 2:
        return s[:2] + "****" + s[-2:]
    return s[:keep] + "****" + s[-keep:]


async def main():
    print("=" * 74)
    print("  LLM Connectivity Test - Real Model Round-Trip")
    print("=" * 74)
    print()

    # Step 1: read env
    api_url = os.getenv("LLM_API_URL", "").strip()
    model_name = os.getenv("LLM_MODEL_NAME", "").strip()
    api_key = os.getenv(
        "DASHSCOPE_API_KEY", os.getenv("OPENAI_API_KEY", "")
    ).strip()
    timeout = int(os.getenv("LLM_TIMEOUT", "120"))
    enable_thinking = (
        os.getenv("LLM_ENABLE_THINKING", "false").lower() == "true"
    )

    key_source = (
        "DASHSCOPE_API_KEY"
        if os.getenv("DASHSCOPE_API_KEY")
        else (
            "OPENAI_API_KEY"
            if os.getenv("OPENAI_API_KEY")
            else "NONE"
        )
    )

    print("[1] LLM_API_URL         =", repr(api_url) if api_url else "[NOT SET]")
    print("[2] LLM_MODEL_NAME      =", repr(model_name) if model_name else "[NOT SET]")
    print("[3] Key source          =", key_source)
    if api_key:
        print("[3a] API Key (masked)   =", mask(api_key))
    else:
        print("[3a] API Key (masked)   = [NOT SET]")
    print("[4] LLM_TIMEOUT         =", timeout, "s")
    print("[5] LLM_ENABLE_THINKING  =", enable_thinking)
    print()

    # Validate
    errors = []
    if not api_url:
        errors.append("LLM_API_URL not set in .env")
    if not model_name:
        errors.append("LLM_MODEL_NAME not set in .env")
    if not api_key:
        errors.append("No API Key. Set DASHSCOPE_API_KEY or OPENAI_API_KEY in .env")

    if errors:
        print("-" * 74)
        print("FAIL: config validation errors")
        for e in errors:
            print("   ", e)
        print("-" * 74)
        print("No silent MockLLMClient fallback exists in this script.")
        sys.exit(1)

    print("OK: config validation passed")
    print()

    # Step 2: init LLMClient
    print("-" * 74)
    print("  Initializing LLMClient (real mode) ...")
    print("-" * 74)
    print()

    class _Config:
        pass

    _Config.api_url = api_url
    _Config.api_key = api_key
    _Config.model_name = model_name
    _Config.timeout = timeout
    _Config.enable_thinking = enable_thinking

    def _get_effective_api_key(self):
        return self.api_key

    _Config.get_effective_api_key = _get_effective_api_key

    from app.integrations.llm_client import LLMClient, MockLLMClient  # noqa: E402

    client = LLMClient(config_provider=_Config())

    # Guard: must NOT be MockLLMClient
    if isinstance(client, MockLLMClient):
        print("FAIL: silent fallback to MockLLMClient detected. Aborting.")
        sys.exit(2)

    cfg = client._resolve_config()
    resolved_url = client._normalize_base_url(cfg.api_url)
    resolved_key = client._resolve_api_key(cfg)
    resolved_model = cfg.model_name.strip()
    resolved_timeout = cfg.timeout

    print(f"    API base URL   : {resolved_url}")
    print(f"    Model          : {resolved_model}")
    print(f"    Timeout        : {resolved_timeout}s")
    print(f"    Enable thinking: {cfg.enable_thinking}")
    print()

    # Step 3: call real model
    print("-" * 74)
    print("  Calling real model ...")
    print("-" * 74)
    print()

    test_prompt = "Please reply with only: connection successful"
    print(f"    Prompt: {test_prompt!r}")
    print()

    error_occurred = False
    error_message = ""
    response_text = ""
    latency_ms = 0.0

    try:
        t0 = time.monotonic()
        response_text = await client.generate(test_prompt)
        t1 = time.monotonic()
        latency_ms = (t1 - t0) * 1000.0
    except Exception as exc:
        error_occurred = True
        error_message = f"{type(exc).__name__}: {exc}"

    # Report
    print("=" * 74)
    print("  Test Report")
    print("=" * 74)
    print()
    print(f"  [a] LLM_API_URL read       : {'Yes' if api_url else 'No'}")
    print(f"  [b] LLM_MODEL_NAME read     : {'Yes' if model_name else 'No'}")
    if api_key:
        print(f"  [c] API Key detected        : Yes ({key_source}, masked: {mask(api_key)})")
    else:
        print(f"  [c] API Key detected        : No")
    print(f"  [d] Real model call ok      : {'No (see error)' if error_occurred else 'Yes'}")
    if response_text:
        resp_display = response_text.strip()[:200]
        print(f"  [e] Model response          : {resp_display!r}")
    else:
        print(f"  [e] Model response          : (empty)")
    print(f"  [f] latency_ms              : {latency_ms:.1f} ms")
    print(f"  [g] Has error               : {'Yes - ' + error_message if error_occurred else 'No'}")
    print(f"  [h] Silent mock fallback    : No (verified at init)")
    print()
    print("-" * 74)
    if error_occurred:
        print("FAIL: connectivity test failed", file=sys.stderr)
        print(f"      {error_message}", file=sys.stderr)
        print("-" * 74, file=sys.stderr)
        sys.exit(1)
    else:
        print("OK: connectivity test passed - real model works")
        print("-" * 74)
    print()


if __name__ == "__main__":
    asyncio.run(main())
