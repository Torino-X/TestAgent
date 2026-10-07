"""Phase 2.8D — GraphState V6 schema(attempt/last_retry_decision/summary) + V2 → V6 迁移。

对应 docs/32 §11 测试矩阵 — 4 cases:
* V6 schema 三字段默认存在
* V2 → V6 迁移补全字段
* 缺关键字段抛 MigrationRequired
* 非 dict 输入抛 MigrationRequired
"""

from __future__ import annotations

import pytest

from app.agent_runtime.checkpoint_state_migration import (
    MigrationRequired,
    V6_NEW_FIELDS,
    V6_REQUIRED_FROM_V2,
    migrate_to_v6,
)
from app.agent_runtime.graphs.test_plan.constants import STATE_SCHEMA_VERSION_V6
from app.agent_runtime.graphs.test_plan.state import make_empty_state


class TestGraphStateV6Schema:
    def test_v6_schema_version_constant(self) -> None:
        """Phase 2.8D:STATE_SCHEMA_VERSION_V6 = 6(沿用 Phase 2.3 V3-2.5 V5 super-set)。"""
        assert STATE_SCHEMA_VERSION_V6 == 6
        # V6 是 V3/V4/V5 的超集
        assert STATE_SCHEMA_VERSION_V6 >= 5

    def test_make_empty_state_includes_v6_fields(self) -> None:
        """Phase 2.8D:make_empty_state(任意路径)都包含 V6 三字段默认值。"""
        s = make_empty_state(task_id="t1", graph_run_id="r1", graph_version="v2")
        assert s["state_schema_version"] == 6
        assert "attempt" in s and s["attempt"] == {}
        assert "last_retry_decision" in s and s["last_retry_decision"] == {}
        assert "summary" in s and s["summary"] is None

    def test_v6_new_fields_constant(self) -> None:
        """Phase 2.8D:V6_NEW_FIELDS 常量暴露 3 字段元数据。"""
        assert set(V6_NEW_FIELDS) == {"attempt", "last_retry_decision", "summary"}
        assert "state_schema_version" in V6_REQUIRED_FROM_V2


class TestCheckpointStateMigration:
    def _valid_v2(self) -> dict:
        return {
            "state_schema_version": 2,
            "current_node": "a",
            "graph_run_id": "g",
            "task_id": "t1",
        }

    def test_migrate_v2_to_v6_includes_defaults(self) -> None:
        """Phase 2.8D:V2 state → V6,补 3 字段 + 升 schema_version。"""
        out = migrate_to_v6(self._valid_v2())
        assert out["state_schema_version"] == 6
        assert out["attempt"] == {}
        assert out["last_retry_decision"] == {}
        assert out["summary"] is None
        # 老字段保留(向后兼容)
        assert out["current_node"] == "a"
        assert out["graph_run_id"] == "g"
        assert out["task_id"] == "t1"

    def test_migrate_v3_to_v6_skips_redundant_defaults(self) -> None:
        """Phase 2.8D:V3 → V6 已存在的 attempt/retry_decision 不被覆盖。"""
        s = self._valid_v2()
        s["state_schema_version"] = 3
        s["attempt"] = {"ToolA": 2}
        s["last_retry_decision"] = {"ToolA": "degrade"}
        s["summary"] = "已有摘要"
        out = migrate_to_v6(s)
        assert out["attempt"] == {"ToolA": 2}
        assert out["last_retry_decision"] == {"ToolA": "degrade"}
        assert out["summary"] == "已有摘要"

    def test_migrate_missing_required_raises(self) -> None:
        """Phase 2.8D:缺关键字段(例如 current_node)→ MigrationRequired。"""
        with pytest.raises(MigrationRequired) as exc:
            migrate_to_v6({"state_schema_version": 2, "task_id": "t"})
        assert "current_node" in str(exc.value)

    def test_migrate_non_dict_raises(self) -> None:
        """Phase 2.8D:非 dict 输入(字符串/列表)直接拒绝。"""
        with pytest.raises(MigrationRequired):
            migrate_to_v6("not a dict")
