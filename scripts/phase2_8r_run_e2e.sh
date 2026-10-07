#!/usr/bin/env bash
# scripts/phase2_8r_run_e2e.sh
# Phase 2.8R-G:跑 14 个跨 worker E2E case
# 依赖:docker compose 已启动(phase2_8r_start_test_infra.sh)
# 退出:全部 pass 返回 0;有 fail 返回非 0

set -e

BACKEND_DIR="${BACKEND_DIR:-$(dirname "$0")/../backend}"

# ── 加载 .env.test ──
if [ -f "$BACKEND_DIR/.env.test" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$BACKEND_DIR/.env.test"
    set +a
    echo "[phase2_8r_run_e2e] Loaded .env.test"
else
    echo "[phase2_8r_run_e2e] WARNING: .env.test not found, using defaults from .env.test.example"
fi

cd "$BACKEND_DIR"
echo "[phase2_8r_run_e2e] Running E2E multi-worker tests..."

# 14 个 E2E case 在 tests/integration/multiworker/
python -m pytest tests/integration/multiworker/ -v --tb=short "$@"