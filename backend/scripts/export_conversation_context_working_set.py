"""Export one conversation's canonical five-category context working set.

This is an operator-facing inspection utility.  It uses the same
``ConversationContextLedgerService`` selection rules as the Context Usage
popover and writes the actual selected content, source references, and
heuristic token totals to Markdown.  It intentionally does not export every
database record: only material currently eligible for the conversation's
working set is included.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Scripts are executed directly from ``backend/scripts`` in the operator
# workflow, so add the backend package root before importing ``app``.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select

from app.common.token_estimator import estimate_tokens
from app.db.session import AsyncSessionLocal, async_engine
from app.models.conversation import Conversation
from app.repositories.conversation_summary_repository import ConversationSummaryRepository
from app.repositories.message_repository import MessageRepository
from app.services.conversation_context_ledger_service import (
    ConversationContextLedgerService,
)


@dataclass(frozen=True)
class Entry:
    reference: str
    label: str
    content: str


def _markdown_code_block(value: str) -> str:
    fence = "~~~"
    while fence in value:
        fence += "~"
    return f"{fence}text\n{value}\n{fence}"


def _render_entries(entries: list[Entry]) -> list[str]:
    if not entries:
        return ["_当前工作集中没有该类别内容。_", ""]
    lines: list[str] = []
    for index, entry in enumerate(entries, start=1):
        lines.extend(
            [
                f"### {index}. {entry.label}",
                "",
                f"- 来源：`{entry.reference}`",
                f"- 估算 tokens：`{estimate_tokens(entry.content)}`",
                "",
                _markdown_code_block(entry.content),
                "",
            ]
        )
    return lines


async def _export(conversation_public_id: str, output_path: Path) -> None:
    try:
      async with AsyncSessionLocal() as session:
        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.public_id == conversation_public_id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise SystemExit(f"Conversation not found: {conversation_public_id}")

        service = ConversationContextLedgerService(session)
        user_id = int(conversation.user_id)
        messages = await MessageRepository(session).list_context_messages_by_conversation(
            user_id=user_id,
            conversation_id=int(conversation.id),
        )
        summary = await ConversationSummaryRepository(session).get_latest_active_by_type(
            int(conversation.id), user_id, "conversation"
        )
        latest_task = await service._latest_task(int(conversation.id), user_id)
        latest_user_text = next(
            (
                str(message.content or "")
                for message in reversed(messages)
                if getattr(message, "role", "") == "user"
                and getattr(message, "message_type", "") == "user_text"
            ),
            "",
        )
        project_package = await service._project_package(
            conversation=conversation,
            user_id=user_id,
            query=latest_user_text,
            task_id=getattr(latest_task, "public_id", None),
        )

        conversation_texts, conversation_refs = service._conversation_working_set(
            messages, summary
        )
        project_texts, project_refs = service._project_document_working_set(
            project_package
        )
        memory_texts, memory_refs = await service._memory_working_set(
            user_id=user_id,
            project_package=project_package,
            query=latest_user_text,
            task=latest_task,
        )
        system_texts, system_refs = service._system_working_set(project_package)
        task_texts, task_refs = service._task_working_set(latest_task)

        categories: list[tuple[str, str, list[str], list[dict[str, Any]]]] = [
            ("对话历史", "conversation_history", conversation_texts, conversation_refs),
            ("项目资料", "project_documents", project_texts, project_refs),
            ("任务上下文", "task_context", task_texts, task_refs),
            ("Memory", "user_memory", memory_texts, memory_refs),
            ("系统指令", "system_instructions", system_texts, system_refs),
        ]
        entries_by_key: dict[str, list[Entry]] = {}
        for title, key, texts, refs in categories:
            entries_by_key[key] = [
                Entry(
                    reference=str(refs[index]) if index < len(refs) else "unknown",
                    label=f"{title}条目 {index + 1}",
                    content=text,
                )
                for index, text in enumerate(texts)
            ]

        breakdown = {
            key: sum(estimate_tokens(entry.content) for entry in entries_by_key[key])
            for _, key, _, _ in categories
        }
        materialized = await service.materialize(
            conversation=conversation,
            user_id=user_id,
            context_window_tokens=200_000,
        )
        computed_total = sum(breakdown.values())
        ledger_matches = breakdown == materialized.breakdown and computed_total == materialized.total_tokens
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        lines = [
            f"# 会话上下文工作集查看：{conversation_public_id}",
            "",
            f"- 导出时间：`{now}`",
            f"- 会话标题：{conversation.title}",
            f"- 当前任务：`{getattr(latest_task, 'public_id', None) or '无'}`",
            f"- 工作集策略：`{materialized.manifest.get('policy_version', 'unknown')}`",
            f"- 上下文窗口：`{materialized.context_window_tokens if hasattr(materialized, 'context_window_tokens') else 200000}` tokens",
            "",
            "## 统计结论", "",
            "这份文件复现的是 Context Usage 卡片当前展示的**会话工作集**：它不是数据库中所有消息、项目文件和记忆的总量。",
            "每个数值使用后端 `estimate_tokens` 启发式估算，因此可准确核对分类选择与内部统计，但不等同于模型供应商账单中的精确 token 数。",
            "",
            "| 使用项 | 选中条目数 | 重新计算 tokens | 账本 tokens | 一致性 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        labels = {key: title for title, key, _, _ in categories}
        for _, key, _, _ in categories:
            lines.append(
                f"| {labels[key]} | {len(entries_by_key[key])} | {breakdown[key]:,} | {materialized.breakdown.get(key, 0):,} | {'一致' if breakdown[key] == materialized.breakdown.get(key, 0) else '不一致'} |"
            )
        lines.extend(
            [
                f"| **合计** | **{sum(len(value) for value in entries_by_key.values())}** | **{computed_total:,}** | **{materialized.total_tokens:,}** | **{'一致' if ledger_matches else '不一致'}** |",
                "",
                "### 统计准确性判断", "",
                (
                    "- **分类统计一致**：本次导出按与界面相同的服务逻辑重新计算，"
                    "结果与持久化账本完全一致。"
                    if ledger_matches
                    else "- **分类统计存在差异**：导出时的重新计算与账本不一致；请检查本文件的条目与账本刷新时间。"
                ),
                "- **口径限制**：项目资料只保留当前查询命中的前 5 个 chunk，每段最多 1,600 字符；Memory 只保留当前范围与查询下的权威排序结果；任务上下文只取最新任务的稳定投影。",
                "- **动态性**：后续发送消息、更新项目资料或记忆后，下一次 Context Usage 刷新会重新选择内容，因此本文件是上述导出时间点的完整快照。",
                "",
                "## 内容明细", "",
            ]
        )
        for title, key, _, _ in categories:
            lines.extend([f"## {title}", ""])
            lines.extend(_render_entries(entries_by_key[key]))

      output_path.parent.mkdir(parents=True, exist_ok=True)
      output_path.write_text("\n".join(lines), encoding="utf-8")
      print(output_path)
    finally:
      # This standalone command owns the async engine.  Dispose it before
      # asyncio.run() closes the event loop so aiomysql can close cleanly.
      await async_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("conversation_id", help="Conversation public ID, e.g. conv_xxx")
    parser.add_argument("--output", required=True, type=Path, help="Markdown output path")
    args = parser.parse_args()
    asyncio.run(_export(args.conversation_id, args.output.resolve()))


if __name__ == "__main__":
    main()
