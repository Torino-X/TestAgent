"""ToolOutputManager — Tool Output 治理：inline / truncate / payload 外置。

CE-02 WP-6：
- ``manage(user_id, task_public_id, tool_call_public_id, *, raw_output, policy)``
  — raw_output 为结构化 DTO（text/bytes/media_type），**binary 不使用 raw_text
  字段** → ``ManagedToolOutput``；
- 持久化写 ``tool_calls`` 扩展列（context_payload_id / output_preview /
  output_char_count / output_estimated_tokens / output_sha256 / output_truncated /
  output_policy_key / output_policy_version / truncation_metadata_json）；
- 阈值由 ``ToolOutputPolicy`` 配置，判定考虑 chars/bytes/tokens/media_type/budget；
- 策略：small→inline；medium→head/tail truncate + preview；large/huge→全文进
  PayloadStorage + preview + payload_ref；binary→metadata-only + ref；
- 校验：sha256、included+omitted==original。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.tool_output import (
    ManagedToolOutput,
    ToolOutputPolicy,
    ToolOutputTruncation,
)
from app.context_engine.models.value_objects import Digest


class TypedToolOutput:
    """结构化 Tool Output DTO（text/bytes/media_type）。

    binary 不使用 raw_text 字段（由 media_type + data 承载）。
    """

    def __init__(
        self,
        *,
        text: str | None = None,
        data: bytes | None = None,
        media_type: str | None = None,
    ) -> None:
        if text is not None and data is not None:
            raise ValueError("text 与 data 不能同时提供")
        if text is None and data is None:
            raise ValueError("text 或 data 必须至少提供一个")
        self.text = text
        self.data = data
        self.media_type = media_type or ("text/plain" if text is not None else "application/octet-stream")

    @property
    def is_binary(self) -> bool:
        return self.data is not None

    @property
    def char_count(self) -> int:
        return len(self.text or "") if self.text is not None else 0

    @property
    def size_bytes(self) -> int:
        if self.data is not None:
            return len(self.data)
        return len((self.text or "").encode("utf-8"))

    def raw_bytes(self) -> bytes:
        if self.data is not None:
            return self.data
        return (self.text or "").encode("utf-8")


class ToolOutputManager:
    """治理 Tool Output 并持久化到 tool_calls 扩展列。"""

    def __init__(
        self,
        payload_storage_service,
        *,
        tool_call_repository_factory=None,
        token_counter=None,
    ) -> None:
        self._payload_storage_service = payload_storage_service
        self._tool_call_repo_factory = tool_call_repository_factory
        self._token_counter = token_counter

    async def manage(
        self,
        user_id: int,
        task_public_id: str,
        tool_call_public_id: str,
        *,
        raw_output: TypedToolOutput,
        policy: ToolOutputPolicy | None = None,
        session_factory=None,
    ) -> ManagedToolOutput:
        policy = policy or ToolOutputPolicy()
        raw_bytes = raw_output.raw_bytes()
        char_count = raw_output.char_count
        size_bytes = raw_output.size_bytes
        sha256 = Digest.of(raw_bytes)
        estimated_tokens = self._estimate(raw_output.text or "")

        # 判定策略（chars / bytes / tokens / media_type / budget）
        if raw_output.is_binary:
            payload_ref = await self._store_payload(
                user_id=user_id,
                tool_call_public_id=tool_call_public_id,
                raw_output=raw_output,
                session_factory=session_factory,
            )
            # binary → metadata-only + durable ref
            return await self._persist(
                user_id, task_public_id, tool_call_public_id,
                raw_output=raw_output,
                policy=policy,
                preview="",
                char_count=char_count,
                size_bytes=size_bytes,
                estimated_tokens=estimated_tokens,
                sha256=sha256,
                truncated=False,
                payload_ref=payload_ref,
                truncation=ToolOutputTruncation(
                    mode="metadata_only",
                    included_char_count=0,
                    omitted_char_count=char_count,
                ),
                session_factory=session_factory,
            )

        # text 类：按 char 长度分级
        if char_count <= policy.inline_char_limit:
            # small → inline
            return await self._persist(
                user_id, task_public_id, tool_call_public_id,
                raw_output=raw_output,
                policy=policy,
                preview=raw_output.text or "",
                char_count=char_count,
                size_bytes=size_bytes,
                estimated_tokens=estimated_tokens,
                sha256=sha256,
                truncated=False,
                payload_ref=None,
                truncation=None,
                session_factory=session_factory,
            )

        text = raw_output.text or ""
        # medium：text 超 inline 限制，但截断预览可代表有意义比例 → head/tail truncate
        preview_budget = policy.head_chars + policy.tail_chars
        if preview_budget and len(text) <= preview_budget * 2:
            # medium → head/tail truncate + preview
            head = text[: policy.head_chars]
            tail = text[-policy.tail_chars:] if policy.tail_chars else ""
            preview = head + (("…" + tail) if tail else "…")
            return await self._persist(
                user_id, task_public_id, tool_call_public_id,
                raw_output=raw_output,
                policy=policy,
                preview=preview,
                char_count=char_count,
                size_bytes=size_bytes,
                estimated_tokens=estimated_tokens,
                sha256=sha256,
                truncated=True,
                payload_ref=None,
                truncation=ToolOutputTruncation(
                    mode="head_tail",
                    included_char_count=len(head) + len(tail),
                    omitted_char_count=char_count - len(head) - len(tail),
                    head_chars=len(head),
                    tail_chars=len(tail),
                ),
                session_factory=session_factory,
            )

        # large / huge → 全文进 PayloadStorage + preview + payload_ref
        preview_source = text[: policy.head_chars]
        preview = preview_source + "…"
        payload_ref = await self._store_payload(
            user_id=user_id,
            tool_call_public_id=tool_call_public_id,
            raw_output=raw_output,
            session_factory=session_factory,
        )

        return await self._persist(
            user_id, task_public_id, tool_call_public_id,
            raw_output=raw_output,
            policy=policy,
            preview=preview,
            char_count=char_count,
            size_bytes=size_bytes,
            estimated_tokens=estimated_tokens,
            sha256=sha256,
            truncated=True,
            payload_ref=payload_ref,
            truncation=ToolOutputTruncation(
                mode="payload_ref",
                included_char_count=len(preview_source),
                omitted_char_count=char_count - len(preview_source),
                head_chars=len(preview_source),
            ),
            session_factory=session_factory,
        )

    # ── 持久化：写 tool_calls 扩展列 ──────────────────────────────────

    async def _persist(
        self,
        user_id: int,
        task_public_id: str,
        tool_call_public_id: str,
        *,
        raw_output: TypedToolOutput,
        policy: ToolOutputPolicy,
        preview: str,
        char_count: int,
        size_bytes: int,
        estimated_tokens: int,
        sha256: Digest,
        truncated: bool,
        payload_ref: str | None,
        truncation: ToolOutputTruncation | None,
        session_factory,
    ) -> ManagedToolOutput:
        # 校验：included + omitted == original
        if truncation is not None:
            assert (
                truncation.included_char_count + truncation.omitted_char_count == char_count
            ), "included + omitted 必须等于 original char count"

        if session_factory is not None and self._tool_call_repo_factory is not None:
            async with session_factory() as session:
                repo = self._tool_call_repo_factory(session)
                await repo.update_extended_columns(
                    tool_call_public_id,
                    user_id,
                    payload_ref=payload_ref,
                    preview=preview,
                    char_count=char_count,
                    estimated_tokens=estimated_tokens,
                    sha256=str(sha256),
                    truncated=truncated,
                    policy_key=policy.policy_key,
                    policy_version=policy.policy_version,
                    truncation_metadata=truncation.model_dump(mode="json") if truncation else None,
                )
                commit = getattr(session, "commit", None)
                if callable(commit):
                    await commit()

        return ManagedToolOutput(
            tool_call_public_id=tool_call_public_id,
            user_id=user_id,
            preview=preview,
            prompt_text=preview,
            output_char_count=char_count,
            output_size_bytes=size_bytes,
            estimated_tokens=estimated_tokens,
            output_sha256=sha256,
            truncated=truncated,
            payload_ref=payload_ref,
            policy=policy,
            truncation_metadata=truncation,
            media_type=raw_output.media_type,
        )

    async def _store_payload(
        self,
        *,
        user_id: int,
        tool_call_public_id: str,
        raw_output: TypedToolOutput,
        session_factory,
    ) -> str:
        """Persist the complete output or fail before claiming a payload ref."""
        if self._payload_storage_service is None or session_factory is None:
            raise_engine_error(
                code="context.tool_output.payload_unavailable",
                detail="Tool output requires durable payload storage",
                stage=ContextEngineStage.PAYLOAD,
                retryable=True,
                recoverable=True,
            )

        from app.context_engine.models.payload import PayloadStoreCommand

        content = raw_output.data if raw_output.data is not None else (raw_output.text or "")
        stored = await self._payload_storage_service.put(
            PayloadStoreCommand(
                user_id=user_id,
                content=content,
                media_type=raw_output.media_type,
                metadata={"tool_call_public_id": tool_call_public_id},
            ),
            session_factory=session_factory,
        )
        return stored.payload_public_id

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# auto-appended module-level note: tool_output manager: 缓存工具产出(token 计费 + LLM 重放)。
