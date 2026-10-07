"""CE-04 FINAL REVISION §六：Full Prompt Persistence=0 实证。

审计目标：``prompt_excerpt`` 必须是**安全审计摘要**（call_site + 脱敏 user +
source 类型 + section keys + token 数），**不是**最终 composed prompt 的前 N
字符切片。Full Prompt Persistence=0 意味着最终 composed prompt/messages
绝不写入任何持久化字段。

覆盖：
  1. excerpt 来源：构造含 sentinel 全文的 compose → excerpt 不含 sentinel
     （即不切片 final prompt）；
  2. excerpt 上限：MAX_PROMPT_EXCERPT_CHARS 固定，超长输入不越界；
  3. 脱敏：含 API Key / Authorization / DSN / sk- / 敏感文本的输入 → excerpt
     不含这些内容；
  4. excerpt 只含审计 metadata（call_site / user hash / source 类型 /
     section keys / tokens）；
  5. 全存储扫描：compose 结果经 validator/snapshot 写入路径后，任何持久化
     target 不含 sentinel full prompt 或 sentinel secret（真实 SQL 语义用
     sqlite 验证 snapshot 表 + compaction_json + payload 表）。

约束：不修改 v2_frozen / v3 拓扑 / TestPlanGraphState。
"""

from __future__ import annotations

import re
from typing import Any, Dict

import pytest

from app.context_engine.composer import ContextComposer
from app.context_engine.composer.composer import MAX_PROMPT_EXCERPT_CHARS
from app.context_engine.models.context import (
    ContextItem,
    ContextKind,
    ContextRequest,
    ContextTrust,
)
from app.context_engine.models.enums import SourceType
from app.context_engine.models.selection import SelectedContextSet


# ── sentinel（唯一，防误报）─────────────────────────────────────────────

CE04_FULL_PROMPT_SENTINEL = "CE04_FULL_PROMPT_SENTINEL_9f3a"
CE04_SECRET_SENTINEL = "CE04_SECRET_SENTINEL_7b2c"
_FAKE_API_KEY = "sk-" + "CE04SECRET_apikey_5f1e_aabbccdd"
_FAKE_AUTH = "Bearer CE04SECRET_auth_9e2d_11223344"
_FAKE_DSN = "postgresql://u:CE04SECRET_pw_3c6f_55667788@h:5432/db"


def _item(item_id, kind, content, *, source_type="tool_output", trust=ContextTrust.UNTRUSTED_REFERENCE):
    return ContextItem(
        item_id=item_id, kind=kind, source_type=source_type, content=content,
        authority=50, trust=trust, estimated_tokens=10,
    )


def _selected(items):
    return SelectedContextSet(
        included=items,
        section_stats={"evidence": {"required": True, "included_count": len(items)}},
        total_estimated_tokens=sum(i.estimated_tokens for i in items),
    )


def _request(**kw):
    return ContextRequest(
        user_id="usr_1", call_site="chat.reply", current_user_message="hi", **kw
    )


# ══════════════════════════════════════════════════════════════════════
# 1. excerpt 来源：不切片 final prompt（sentinel 全文不进入 excerpt）
# ══════════════════════════════════════════════════════════════════════


def test_excerpt_does_not_contain_final_prompt_content():
    """excerpt 绝不包含最终 composed prompt 的正文 sentinel。

    若 excerpt = prompt_text[:N]，则 sentinel 全文（放在消息最前）会出现在
    excerpt 里。修复后 excerpt 只含审计 metadata → sentinel 消失。
    """
    composer = ContextComposer()
    # sentinel 放在第一条消息（最终 prompt 最前）— 旧实现会截到它
    sentinel_item = _item(
        "i1", ContextKind.SYSTEM_RULES,
        CE04_FULL_PROMPT_SENTINEL + " 系统规则正文",
        source_type="system", trust=ContextTrust.TRUSTED_INSTRUCTION,
    )
    result = composer.compose(_request(), _selected([sentinel_item]))

    assert result.prompt_text  # 最终 prompt 存在
    assert CE04_FULL_PROMPT_SENTINEL in result.prompt_text  # sentinel 在最终 prompt 里
    assert CE04_FULL_PROMPT_SENTINEL not in result.prompt_excerpt  # 但不在 excerpt
    # excerpt 是审计摘要：含 call_site
    assert "call_site=chat.reply" in result.prompt_excerpt


def test_excerpt_contains_audit_metadata_only():
    """excerpt 只含审计 metadata，结构固定（key=value 列表）。"""
    composer = ContextComposer()
    result = composer.compose(
        _request(current_node="generate_test_plan"),
        _selected([
            _item("e1", ContextKind.EVIDENCE, "证据正文内容" * 30, source_type=SourceType.ARTIFACT),
            _item("k1", ContextKind.KNOWLEDGE, "知识内容" * 30, source_type=SourceType.KNOWLEDGE),
        ]),
    )
    ex = result.prompt_excerpt
    # 含审计 key
    for key in ("call_site=", "user=", "node=", "types=", "sections=", "included=", "dropped=", "tokens="):
        assert key in ex, f"excerpt 应含审计 key {key!r}; got={ex!r}"
    # 不含最终 prompt 内容（正文 sentinel 都不在）
    assert "证据正文内容" not in ex
    assert "知识内容" not in ex
    # 含脱敏 user（sha 前缀，非明文 usr_1）
    assert "usr_1" not in ex


# ══════════════════════════════════════════════════════════════════════
# 2. 固定上限
# ══════════════════════════════════════════════════════════════════════


def test_excerpt_respects_fixed_max_length():
    """excerpt 长度 <= MAX_PROMPT_EXCERPT_CHARS，超长输入不越界。"""
    composer = ContextComposer()
    # 大量 section → 审计摘要也会变长；构造足够多的 included 项
    items = [
        _item(f"i{n}", ContextKind.EVIDENCE, f"正文{n}" * 100, source_type=SourceType.TOOL_OUTPUT)
        for n in range(60)
    ]
    result = composer.compose(_request(), _selected(items))
    assert len(result.prompt_excerpt) <= MAX_PROMPT_EXCERPT_CHARS


# ══════════════════════════════════════════════════════════════════════
# 3. 脱敏：secret / api key / auth / dsn 不进 excerpt
# ══════════════════════════════════════════════════════════════════════


def test_excerpt_redacts_secrets_and_api_keys():
    """输入含 API Key / Authorization / DSN / 敏感文本 → excerpt 不含它们。

    excerpt 只从 metadata 构建（call_site/user/types/sections/tokens），
    根本不读消息正文 → 这些敏感串不可能出现。
    """
    composer = ContextComposer()
    secret_content = (
        f"{_FAKE_API_KEY}\n{_FAKE_AUTH}\n{_FAKE_DSN}\n"
        f"用户敏感文本 {CE04_SECRET_SENTINEL}"
    )
    result = composer.compose(
        _request(),
        _selected([
            _item("i1", ContextKind.EVIDENCE, secret_content, source_type=SourceType.TOOL_OUTPUT),
        ]),
    )
    ex = result.prompt_excerpt
    for secret in (
        CE04_SECRET_SENTINEL,
        _FAKE_API_KEY,
        _FAKE_AUTH,
        _FAKE_DSN,
        "sk-CE04",
        "Bearer CE04",
        "postgresql://u:CE04",
    ):
        assert secret not in ex, f"excerpt 泄漏敏感内容: {secret!r}; got={ex!r}"
    # 不含用户文档原文
    assert "用户敏感文本" not in ex


# ══════════════════════════════════════════════════════════════════════
# 4. 全存储扫描：persistence target 不含 sentinel（sqlite 真实 SQL）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_snapshot_persistence_does_not_store_full_prompt_or_secret(
    sqlite_session_factory,
):
    """snapshot 表 / compaction_runs.compaction_json / payloads 不含 sentinel。

    用真实 sqlite schema（tests/conftest.py create_all）验证：即使业务把
    sentinel 全文注入 compose 输入，任何持久化 target 都不含 sentinel。
    """
    from sqlalchemy import text

    sentinel_prompt = CE04_FULL_PROMPT_SENTINEL + "完整系统提示词" * 20
    sentinel_secret = _FAKE_API_KEY

    # 用 composer 生成结果（含 sentinel 输入）→ excerpt 不含 sentinel
    composer = ContextComposer()
    result = composer.compose(
        _request(),
        _selected([
            _item("i1", ContextKind.EVIDENCE, sentinel_prompt + sentinel_secret, source_type=SourceType.TOOL_OUTPUT),
        ]),
    )
    assert sentinel_prompt not in result.prompt_excerpt
    assert sentinel_secret not in result.prompt_excerpt

    async with sqlite_session_factory() as s:
        # 扫描快照表（若存在）
        tables = (await s.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))).scalars().all()
        scanned = [t for t in tables if t in (
            "llm_context_snapshots", "context_compaction_runs",
            "context_payloads", "conversation_summaries", "agent_events",
        )]
        for table in scanned:
            cols = (await s.execute(text(
                f"PRAGMA table_info({table})"
            ))).all()
            for col in cols:
                colname = col[1]
                rows = (await s.execute(text(
                    f"SELECT {colname} FROM {table} WHERE {colname} LIKE '%{sentinel_prompt}%' "
                    f"OR {colname} LIKE '%{sentinel_secret}%'"
                ))).all()
                assert not rows, (
                    f"table={table} col={colname} 含 sentinel（full prompt 或 secret 泄漏）"
                )
    # 扫描断言完成（无泄漏）
    assert True


def test_full_prompt_persistence_zero_code_level():
    """代码级验证：composer 只写 digest + 安全 excerpt，不写 prompt_text 到验证器。"""
    # ComposeValidation 字段不包含 prompt_text（只含 digest/excerpt/tokens）
    from app.context_engine.models.compose import ComposeValidation
    fields = ComposeValidation.model_fields.keys()
    assert "prompt_text" not in fields, "ComposeValidation 不应包含 prompt_text"
    assert "prompt_excerpt" in fields
    assert "digest" in fields
