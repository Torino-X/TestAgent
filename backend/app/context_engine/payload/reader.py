"""PayloadReader — 按 ContextPayloadRef 读取并重估 token。

CE-02 WP-5：供恢复 / Ref 读取；owner-scope。
"""

from __future__ import annotations

from app.context_engine.models.payload import ContextPayloadRef


class PayloadReader:
    """按 ContextPayloadRef 读取内容 + 重估 token。"""

    def __init__(self, storage_service, *, token_counter=None) -> None:
        self._storage_service = storage_service
        self._token_counter = token_counter

    async def read_text(self, ref: ContextPayloadRef, *, session_factory) -> str:
        """读取 payload 内容为文本。"""
        chunks = []
        async for chunk in self._storage_service.open(
            ref.owner_user_id, ref.payload_public_id, session_factory=session_factory
        ):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def estimate_tokens(self, ref: ContextPayloadRef, *, session_factory) -> int:
        """读取并重估 token 数。"""
        text = await self.read_text(ref, session_factory=session_factory)
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)
# auto-appended module-level note: payload reader: 按 storage_key 读回 + decode。
