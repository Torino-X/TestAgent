#!/usr/bin/env bash
# scripts/prod_logs.sh [service-name]
# 默认看 backend;可以 `bash scripts/prod_logs.sh postgres` 看 PG 日志

set -e

cd "$(dirname "$0")/.."

SERVICE="${1:-backend}"

echo "[prod_logs] Tailing $SERVICE (-f, Ctrl-C to exit)..."
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f --tail=100 "$SERVICE"