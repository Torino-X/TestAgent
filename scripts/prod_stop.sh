#!/usr/bin/env bash
# scripts/prod_stop.sh

set -e

cd "$(dirname "$0")/.."

docker compose -f docker-compose.prod.yml --env-file .env.prod down

echo "[prod_stop] Done."

# Note: 默认**不**带 -v(不删 volumes,保留 PG / Redis 数据)
#       如果要清空数据:docker compose down -v(危险,确认后再用)