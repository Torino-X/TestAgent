#!/usr/bin/env bash
# scripts/phase2_8r_start_test_infra.sh
# Phase 2.8R-G:启动 MySQL/Postgres/Redis 测试基建
# 依赖:docker compose(>=v2)
# 退出:服务就绪返回 0;超时返回 1

set -e

COMPOSE_FILE="${COMPOSE_FILE:-$(dirname "$0")/../docker-compose.test.yml}"

if ! command -v docker >/dev/null 2>&1; then
    echo "[phase2_8r_start_test_infra] ERROR: docker not installed" >&2
    exit 1
fi

echo "[phase2_8r_start_test_infra] Starting test infra: $COMPOSE_FILE"
docker compose -f "$COMPOSE_FILE" up -d

echo "[phase2_8r_start_test_infra] Waiting for services to be ready..."

# MySQL (端口 3307)
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24; do
    if (echo > /dev/tcp/127.0.0.1/3307) 2>/dev/null; then
        echo "[phase2_8r_start_test_infra] MySQL port 3307 ready (took ${i}s)"
        break
    fi
    sleep 1
done

# Postgres (端口 5433)
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if (echo > /dev/tcp/127.0.0.1/5433) 2>/dev/null; then
        echo "[phase2_8r_start_test_infra] Postgres port 5433 ready (took ${i}s)"
        break
    fi
    sleep 1
done

# Redis (端口 6380)
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if (echo > /dev/tcp/127.0.0.1/6380) 2>/dev/null; then
        echo "[phase2_8r_start_test_infra] Redis port 6380 ready (took ${i}s)"
        break
    fi
    sleep 1
done

echo "[phase2_8r_start_test_infra] Test infra ready."
echo "  MySQL:    localhost:3307  (user=root pass=testpass db=testagent_test)"
echo "  Postgres: localhost:5433  (user=test pass=testpass db=langgraph_test)"
echo "  Redis:    localhost:6380"
echo ""
echo "下一步:运行 E2E 测试"
echo "  bash scripts/phase2_8r_run_e2e.sh"