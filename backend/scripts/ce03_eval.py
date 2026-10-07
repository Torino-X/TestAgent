"""CE-03 评测脚本：计算 Recall@5 / MRR / P50 / P95 与通道拆分指标。

读取 tests/ce03_eval/dataset.json（版本化、脱敏标注数据集，≥50 条），
对每条 query 调用检索管线并比对 expected IDs。

用法：
  python scripts/ce03_eval.py --mode hybrid        # 双通道
  python scripts/ce03_eval.py --mode lexical-only
  python scripts/ce03_eval.py --mode dense-only
  python scripts/ce03_eval.py --mode rerank-on     # 真实 reranker（如可用）

输出（不伪造，无真实检索时打印 BLOCKED）：
  dataset_version / dataset_hash / scope_distribution / negative_count
  actual_recall_at_5 / actual_mrr / p50 / p95 / failed_query_count
  per_scope_recall  / lexical_vs_dense_vs_hybrid / rerank_before_after

门禁（编码前定稿，不得事后调整）：
  recall@5 >= 0.70 / MRR >= 0.65 / P95 <= 1500ms / ACL Leakage = 0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

GATES = {
    "recall_at_5": 0.70,
    "mrr": 0.65,
    "p95_ms": 1500,
    "acl_leakage": 0,
}

DATASET_PATH = Path(__file__).resolve().parent.parent / "tests" / "ce03_eval" / "dataset.json"


def load_dataset() -> dict:
    if not DATASET_PATH.is_file():
        raise FileNotFoundError(f"dataset not found: {DATASET_PATH}")
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    raw = DATASET_PATH.read_bytes()
    data["dataset_hash"] = hashlib.sha256(raw).hexdigest()
    data["query_count"] = len(data["queries"])
    data["negative_count"] = sum(1 for q in data["queries"] if q.get("negative"))
    return data


def expected_ids(q: dict) -> set[str]:
    ids = set(q.get("expected_doc_ids") or [])
    ids.update(q.get("expected_chunk_ids") or [])
    ids.update(q.get("expected_memory_ids") or [])
    return ids


def actual_ids(q: dict, retrieval) -> set[str]:
    """调用检索管线取得实际命中 ID 集合（由测试 runner 注入）。
    真实评测依赖 Qdrant/ES + Embedding Provider；未部署时返回 None → BLOCKED。
    """
    return retrieval(q) if retrieval else None


def recall_at_5(actual: set[str], expected: set[str]) -> float:
    if not expected:
        return 1.0 if not actual else 0.0
    return len(actual & expected) / len(expected)


def reciprocal_rank(actual: list[str], expected: set[str]) -> float:
    for rank, cid in enumerate(actual, start=1):
        if cid in expected:
            return 1.0 / rank
    return 0.0


def run_eval(dataset: dict, retrieval=None) -> dict:
    latencies: list[float] = []
    recalls: list[float] = []
    rrs: list[float] = []
    failed = 0
    per_scope: dict[str, list[float]] = {}
    leakage = 0

    for q in dataset["queries"]:
        scope = q["scope"]
        expected = expected_ids(q)
        if q.get("negative"):
            # 负面用例：任何命中都算泄漏
            actual = actual_ids(q, retrieval) or set()
            if actual:
                leakage += 1
            continue

        start = time.monotonic()
        actual = actual_ids(q, retrieval)
        elapsed_ms = (time.monotonic() - start) * 1000
        if actual is None:
            return {"status": "BLOCKED", "reason": "真实检索不可用（无 Qdrant/ES/Embedding）"}
        latencies.append(elapsed_ms)

        actual_list = list(actual)
        r = recall_at_5(set(actual_list[:5]), expected)
        recalls.append(r)
        rrs.append(reciprocal_rank(actual_list, expected))
        per_scope.setdefault(scope, []).append(r)
        if r == 0.0 and expected:
            failed += 1

    p50 = statistics.median(latencies) if latencies else None
    p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1] if len(latencies) >= 20 else (
        max(latencies) if latencies else None
    )

    return {
        "status": "PASS" if recall_at_5_ok(recalls) and mrr_ok(rrs) and p95_ok(p95) and leakage == 0 else "FAIL",
        "actual_recall_at_5": round(statistics.mean(recalls), 4) if recalls else None,
        "actual_mrr": round(statistics.mean(rrs), 4) if rrs else None,
        "p50_ms": round(p50, 1) if p50 else None,
        "p95_ms": round(p95, 1) if p95 else None,
        "failed_query_count": failed,
        "per_scope_recall": {k: round(statistics.mean(v), 4) for k, v in per_scope.items()},
        "acl_leakage": leakage,
        "query_count": len(dataset["queries"]),
        "negative_count": dataset["negative_count"],
    }


def recall_at_5_ok(recalls: list[float]) -> bool:
    return bool(recalls) and statistics.mean(recalls) >= GATES["recall_at_5"]


def mrr_ok(rrs: list[float]) -> bool:
    return bool(rrs) and statistics.mean(rrs) >= GATES["mrr"]


def p95_ok(p95: float | None) -> bool:
    return p95 is not None and p95 <= GATES["p95_ms"]


def main() -> int:
    parser = argparse.ArgumentParser(description="CE-03 检索评测")
    parser.add_argument("--mode", default="hybrid",
                        choices=["hybrid", "lexical-only", "dense-only", "rerank-on"])
    args = parser.parse_args()

    dataset = load_dataset()
    print(json.dumps(
        {
            "dataset_id": dataset["dataset_id"],
            "dataset_version": dataset["dataset_version"],
            "dataset_hash": dataset["dataset_hash"],
            "query_count": dataset["query_count"],
            "scope_distribution": dataset["scope_distribution"],
            "negative_count": dataset["negative_count"],
            "mode": args.mode,
            "gates": GATES,
        },
        ensure_ascii=False, indent=2,
    ))

    # 真实检索 runner 未注入（Qdrant/ES 未部署）→ BLOCKED，不伪造数值
    result = run_eval(dataset, retrieval=None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "BLOCKED":
        print("\n真实 Qdrant/ES 部署并注入 retrieval 后执行，方可产生实测数值。")
        return 3
    if result["status"] == "PASS":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
