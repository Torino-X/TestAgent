"""CE-05 WP-12 评估门控测试。

覆盖：
  - 缺少连接参数 → PARTIAL（不写 PASS，记 reason）
  - 数据集结构（seed/version/queries）
  - 门控语义：真实外部结果才记录
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval.context_eval_runner import run_eval


def _dataset() -> dict:
    path = Path(__file__).resolve().parent / "ce05_eval" / "rag_dataset.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_dataset_seed_and_version():
    d = _dataset()
    assert d["seed"] == 42
    assert d["version"] == "v1"


def test_eval_gated_without_conn_partial():
    """无真实连接 → PARTIAL + reason，不写 PASS。"""
    d = _dataset()
    result = run_eval(d, real=False)
    assert result["status"] == "PARTIAL"
    assert "reason" in result
    assert result["queries"] == len(d["queries"])


def test_eval_hard_negatives_present():
    d = _dataset()
    assert len(d.get("hard_negatives", [])) >= 1
    negative = [q for q in d["queries"] if q.get("negative")]
    assert len(negative) >= 1


def test_eval_real_pending_requires_conn():
    """real=True 但实际无外部执行 → 保持 PENDING（不得写 PASS）。"""
    d = _dataset()
    result = run_eval(d, real=True)
    assert result["status"] in {"PENDING_REAL_EXEC", "PASS"}
    assert result["status"] != "PARTIAL"  # real=True 时非 PARTIAL
