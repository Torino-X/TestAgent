#!/usr/bin/env bash
# scripts/phase2_8r_stop_test_infra.sh
# Phase 2.8R-G:停止 MySQL/Postgres/Redis 测试基建

set -e

COMPOSE_FILE="${COMPOSE_FILE:-$(dirname "$0")/../docker-compose.test.yml}"

echo "[phase2_8r_stop_test_infra] Stopping test infra: $COMPOSE_FILE"
docker compose -f "$COMPOSE_FILE" down -v
echo "[phase2_8r_stop_test_infra] Done."