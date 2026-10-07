"""Seed an LLM model_config row for a given user (F015 test helper).

Usage::

    python -m scripts.seed_user_model_config --username e2e_test \\
        --api-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \\
        --model-name qwen3.7-plus --timeout 600

The API key is read from the environment variable ``SEED_LLM_API_KEY``
(never as a CLI arg so the secret doesn't end up in shell history).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select

from app.core.crypto import encrypt_api_key
from app.db.session import AsyncSessionLocal
from app.models.config import ModelConfig
from app.models.user import User
from app.repositories.model_config_repository import ModelConfigRepository


async def main_async(username: str, api_base_url: str, model_name: str,
                      timeout: int, enable_thinking: bool) -> None:
    api_key = os.environ.get("SEED_LLM_API_KEY", "").strip()
    if not api_key:
        print("ERROR: SEED_LLM_API_KEY env var is empty — refusing to write a config with no key.",
              file=sys.stderr)
        sys.exit(1)

    async with AsyncSessionLocal() as session:
        user = (await session.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if user is None:
            print(f"ERROR: user {username!r} not found", file=sys.stderr)
            sys.exit(2)

        repo = ModelConfigRepository(session)
        encrypted = encrypt_api_key(api_key)
        # Mark all existing rows for this user as non-default (defensive)
        existing = await repo.get_active_for_user(user.id)
        if existing is not None:
            print(f"Replacing existing config id={existing.id} for user_id={user.id}")
        saved = await repo.upsert_for_user(
            user_id=user.id,
            public_id=existing.public_id if existing else None,
            config_name="E2E 模型配置",
            provider="openai-compatible",
            api_base_url=api_base_url,
            api_key_encrypted=encrypted,
            model_name=model_name,
            timeout_seconds=timeout,
            enable_thinking=enable_thinking,
            temperature=None,
            max_tokens=None,
        )
        await session.commit()
        print(f"OK: model_config id={saved.id} for user_id={user.id} ({username})")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--username", required=True)
    p.add_argument("--api-base-url", required=True)
    p.add_argument("--model-name", required=True)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--enable-thinking", action="store_true")
    args = p.parse_args()
    asyncio.run(main_async(
        username=args.username,
        api_base_url=args.api_base_url,
        model_name=args.model_name,
        timeout=args.timeout,
        enable_thinking=args.enable_thinking,
    ))


if __name__ == "__main__":
    main()
