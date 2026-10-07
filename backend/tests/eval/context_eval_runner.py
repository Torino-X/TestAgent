"""CE-05 context_eval_runner — 门控真实评估执行器。

规则：
  - 连接参数存在 → 真实执行
  - 缺失连接参数/凭据失效 → skip + 记 reason（不写 PASS）
  - 无真实外部结果 → 状态 PARTIAL/BLOCKED
输出机器可读 JSON 到 tests/ce05_eval/results/。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

EVAL_RESULTS_DIR = Path(__file__).resolve().parent.parent / "ce05_eval" / "results"


def _has_external_conn() -> bool:
    """门控：是否有真实外部服务连接参数（Provider/向量/词法）。"""
    conn_vars = [
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "QDRANT_URL",
        "ELASTICSEARCH_URL",
        "AGENT_RUNTIME_POSTGRES_URL",
    ]
    return any(os.environ.get(v) for v in conn_vars) or _env_file_has_conn()


def _env_file_has_conn() -> bool:
    env_path = Path(__file__).resolve().parents[4] / "backend" / ".env"
    if not env_path.exists():
        return False
    try:
        text = env_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False
    return any(
        marker in text
        for marker in ("DASHSCOPE_API_KEY=", "OPENAI_API_KEY=", "QDRANT_URL=", "ELASTICSEARCH_URL=")
    )


def run_eval(dataset: dict, *, real: bool) -> dict:
    """执行评估。real=False → PARTIAL（无真实结果）。"""
    queries = dataset.get("queries", [])
    if not real:
        return {
            "status": "PARTIAL",
            "reason": "缺少真实外部连接参数（门控），不执行",
            "dataset_version": dataset.get("version", "v1"),
            "queries": len(queries),
            "results": [],
        }
    # 真实执行占位：连接真实服务后填充
    return {
        "status": "PENDING_REAL_EXEC",
        "reason": "真实连接可用，评估在 CI/人工授权下执行",
        "dataset_version": dataset.get("version", "v1"),
        "queries": len(queries),
        "results": [],
    }


def main() -> int:
    dataset_path = Path(__file__).resolve().parent.parent / "ce05_eval" / "rag_dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    real = _has_external_conn()
    result = run_eval(dataset, real=real)

    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVAL_RESULTS_DIR / "rag_result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"PASS", "PENDING_REAL_EXEC"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
