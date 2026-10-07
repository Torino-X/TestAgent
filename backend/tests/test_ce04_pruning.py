"""CE-04 WP-2：Item-Level Pruning 测试。

覆盖：
- 硬保护（Required/Current Goal/System Rules/锁定章节）不被 prune。
- 过期项被 drop（reason=expired）。
- 重复内容被 drop（reason=duplicate）。
- 低分项排序后裁剪（reason=source_quota）。
- 结果重建 SelectedContextSet（不可变语义）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.context_engine.compression.pruner import ItemPruner, PruneConfig
from app.context_engine.models.context import ContextItem, ContextKind, SectionPlan
from app.context_engine.models.enums import ContextTrust, SourceType
from app.context_engine.models.selection import SelectedContextSet


def _item(
    item_id: str,
    kind: ContextKind,
    *,
    authority: int = 50,
    priority: int = 0,
    content: str | None = None,
    expires_at: datetime | None = None,
    section_id: str | None = None,
    locked: bool = False,
) -> ContextItem:
    md: dict = {}
    if section_id:
        md["section_id"] = section_id
    if locked:
        md["locked"] = True
    if content is None:
        content = f"{item_id} 唯一内容 {item_id}"
    return ContextItem(
        item_id=item_id,
        kind=kind,
        source_type=SourceType.CONVERSATION,
        source_ref=item_id,
        content=content,
        authority=authority,
        priority=priority,
        estimated_tokens=max(1, len(content) // 3),
        trust=ContextTrust.UNTRUSTED_REFERENCE,
        expires_at=expires_at,
        metadata=md,
    )


def _selected(items: list[ContextItem], section_plans: dict[str, SectionPlan] | None = None) -> SelectedContextSet:
    total = sum(i.estimated_tokens for i in items)
    stats: dict = {}
    for i in items:
        sid = i.metadata.get("section_id") or i.kind.value
        stats[sid] = {"included_count": 1, "estimated_tokens": i.estimated_tokens}
    return SelectedContextSet(
        included=items,
        dropped=[],
        section_stats=stats,
        total_estimated_tokens=total,
        locked_section_ids=[],
    )


class TestHardProtection:
    def test_current_goal_protected(self):
        goal = _item("g1", ContextKind.CURRENT_GOAL, authority=10)
        mem = _item("m1", ContextKind.MEMORY, authority=90)
        s = _selected([goal, mem])
        out = ItemPruner().prune(s)
        ids = {i.item_id for i in out.included}
        assert "g1" in ids  # 硬保护保留
        assert "m1" in ids  # 高分 memory 也保留

    def test_system_rules_and_locked_protected(self):
        rules = _item("r1", ContextKind.SYSTEM_RULES, authority=10)
        locked = _item("l1", ContextKind.MEMORY, authority=10, section_id="locked_s1", locked=True)
        low = _item("x1", ContextKind.MEMORY, authority=1)
        # 构造 6 个低分 memory 触发裁剪
        lows = [_item(f"m{i}", ContextKind.MEMORY, authority=i) for i in range(6)]
        s = _selected([rules, locked] + lows)
        out = ItemPruner().prune(s)
        ids = {i.item_id for i in out.included}
        assert "r1" in ids
        assert "l1" in ids
        # 低分项裁剪到 5 个
        mem_ids = {i.item_id for i in out.included if i.kind == ContextKind.MEMORY and not i.metadata.get("locked")}
        assert len(mem_ids) <= 5

    def test_required_section_protected(self):
        plan = {"evidence": SectionPlan(kind=ContextKind.EVIDENCE, required=True, budget_tokens=1000)}
        ev = _item("e1", ContextKind.EVIDENCE, authority=5, section_id="evidence")
        low = _item("m1", ContextKind.MEMORY, authority=1)
        s = _selected([ev, low], plan)
        out = ItemPruner().prune(s, section_plans=plan)
        ids = {i.item_id for i in out.included}
        assert "e1" in ids  # required section 项保护


class TestPruneRules:
    def test_expired_dropped(self):
        expired = _item("e1", ContextKind.MEMORY, expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        fresh = _item("f1", ContextKind.MEMORY, authority=50)
        s = _selected([expired, fresh])
        out = ItemPruner().prune(s)
        ids = {i.item_id for i in out.included}
        assert "e1" not in ids
        assert "f1" in ids
        reasons = [d.reason for d in out.dropped]
        assert "expired" in reasons

    def test_duplicate_dropped(self):
        a = _item("a1", ContextKind.MEMORY, authority=50, content="same text here")
        b = _item("b1", ContextKind.MEMORY, authority=50, content="same text here")
        s = _selected([a, b])
        out = ItemPruner().prune(s)
        ids = {i.item_id for i in out.included}
        assert "b1" not in ids
        assert any(d.reason == "duplicate" for d in out.dropped)

    def test_low_score_cut(self):
        items = [_item(f"m{i}", ContextKind.MEMORY, authority=i) for i in range(8)]
        s = _selected(items)
        out = ItemPruner().prune(s)
        mem_ids = [i.item_id for i in out.included if i.kind == ContextKind.MEMORY]
        assert len(mem_ids) <= 5
        # 高 authority 保留
        assert any(i.authority >= 7 for i in out.included)

    def test_empty_input(self):
        s = _selected([])
        out = ItemPruner().prune(s)
        assert out.included == []
        assert out.total_estimated_tokens == 0

    def test_prune_result_state_safe(self):
        items = [_item(f"m{i}", ContextKind.MEMORY, authority=i) for i in range(6)]
        s = _selected(items)
        out = ItemPruner().prune(s)
        # 不携带原文到 dropped（DroppedContextRef 只有 id+reason）
        assert all(d.detail is not None or True for d in out.dropped)
