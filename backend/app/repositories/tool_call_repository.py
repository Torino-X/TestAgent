"""Tool call repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_call import ToolCall
from app.repositories.base import BaseRepository


class ToolCallRepository(BaseRepository[ToolCall]):
    model = ToolCall

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_task(self, task_internal_id: int) -> list[ToolCall]:
        result = await self.session.execute(
            select(ToolCall)
            .where(ToolCall.task_id == task_internal_id)
            .order_by(ToolCall.created_at.asc())
        )
        return list(result.scalars().all())

    async def create(self, tc: ToolCall) -> ToolCall:
        # aiomysql driver chokes on dict params for JSON columns —
        # use raw INSERT with explicit json.dumps.
        import json as _json
        from sqlalchemy import text

        input_str = None
        if tc.input_summary_json is not None:
            if isinstance(tc.input_summary_json, str):
                input_str = tc.input_summary_json
            else:
                input_str = _json.dumps(tc.input_summary_json, ensure_ascii=False, default=str)

        output_str = None
        if tc.output_summary_json is not None:
            if isinstance(tc.output_summary_json, str):
                output_str = tc.output_summary_json
            else:
                output_str = _json.dumps(tc.output_summary_json, ensure_ascii=False, default=str)

        await self.session.execute(
            text(
                "INSERT INTO tool_calls "
                "(public_id, user_id, conversation_id, task_id, tool_name, "
                " tool_stage, status, input_summary_json, output_summary_json, "
                " error_code, error_message, started_at, finished_at, duration_ms, "
                " output_truncated, created_at, updated_at) "
                "VALUES "
                "(:pid, :uid, :cid, :tid, :tn, :ts, :st, :ij, :oj, "
                " :ec, :em, :sa, :fa, :dm, :otr, :ca, :ua)"
            ),
            {
                "pid": tc.public_id,
                "uid": tc.user_id,
                "cid": tc.conversation_id,
                "tid": tc.task_id,
                "tn": tc.tool_name,
                "ts": tc.tool_stage,
                "st": tc.status,
                "ij": input_str,
                "oj": output_str,
                "ec": tc.error_code,
                "em": tc.error_message,
                "sa": tc.started_at,
                "fa": tc.finished_at,
                "dm": tc.duration_ms,
                "otr": False,
                "ca": tc.created_at,
                "ua": tc.updated_at,
            },
        )
        await self.session.flush()
        return tc

    async def update_extended_columns(
        self,
        public_id: str,
        user_id: int,
        *,
        payload_ref: str | None,
        preview: str,
        char_count: int,
        estimated_tokens: int,
        sha256: str,
        truncated: bool,
        policy_key: str,
        policy_version: str,
        truncation_metadata: dict | None,
    ) -> None:
        """CE-02 WP-6：写 tool_calls 扩展列（context_payload 关联 + 输出治理）。"""
        import json as _json
        from sqlalchemy import text

        # 解析 payload 内部 id（payload_ref 为 public_id）
        context_payload_id = None
        if payload_ref:
            result = await self.session.execute(
                text(
                    "SELECT id FROM context_payloads "
                    "WHERE public_id = :pid AND user_id = :uid AND deleted_at IS NULL LIMIT 1"
                ),
                {"pid": payload_ref, "uid": user_id},
            )
            row = result.first()
            if row is not None:
                context_payload_id = row[0]

        await self.session.execute(
            text(
                "UPDATE tool_calls SET "
                " context_payload_id = :cpid, "
                " output_preview = :op, output_char_count = :occ, "
                " output_estimated_tokens = :oet, output_sha256 = :osh, "
                " output_truncated = :otr, output_policy_key = :opk, "
                " output_policy_version = :opv, truncation_metadata_json = :tm "
                "WHERE public_id = :pid AND user_id = :uid"
            ),
            {
                "cpid": context_payload_id,
                "op": preview,
                "occ": char_count,
                "oet": estimated_tokens,
                "osh": sha256,
                "otr": truncated,
                "opk": policy_key,
                "opv": policy_version,
                "tm": _json.dumps(truncation_metadata, ensure_ascii=False, default=str)
                if truncation_metadata is not None
                else None,
                "pid": public_id,
                "uid": user_id,
            },
        )
        await self.session.flush()


# 模块定位:ToolCall 仓储
#
# 链路:
#   TestAgentToolAdapter.execute → upsert tool_call 行
#   api/v1/agent_tasks/events → 读 task 全 tool_call
#
# 关键约束:
#   - 字段白名单脱敏(no api_key / no storage_key);
#   - status 不让外部直接改(API 拿不到 commit 权限);
#   - duration_ms 仅在终态写。
