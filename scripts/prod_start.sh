#!/usr/bin/env bash
# scripts/prod_start.sh
# 生产栈启动:Postgres / Redis / Backend / Frontend / Nginx
# 内部网络,**只暴露** nginx 80/443 到主机(loopback by default)

set -e

cd "$(dirname "$0")/.."

if [ ! -f .env.prod ]; then
    echo "ERROR: .env.prod not found" >&2
    echo "       copy from .env.test.example, fill DATABASE_URL + POSTGRES_PASSWORD + REDIS_PASSWORD" >&2
    exit 1
fi

# 必须密文环境变量在 .env.prod 中存在
grep -q "^POSTGRES_PASSWORD=" .env.prod || {
    echo "ERROR: POSTGRES_PASSWORD missing in .env.prod" >&2; exit 1; }
grep -q "^DATABASE_URL=" .env.prod || {
    echo "ERROR: DATABASE_URL missing in .env.prod" >&2; exit 1; }
grep -q "^REDIS_PASSWORD=" .env.prod || {
    echo "ERROR: REDIS_PASSWORD missing in .env.prod" >&2; exit 1; }

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not installed" >&2
    echo "       云服务器上:`curl -fsSL https://get.docker.com | bash`" >&2
    exit 1
fi

echo "[prod_start] Pulling images..."
docker compose -f docker-compose.prod.yml --env-file .env.prod pull

echo "[prod_start] Starting stack..."
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d

echo "[prod_start] Waiting for Postgres / Redis healthchecks..."
sleep 15

echo "[prod_start] State:"
docker compose -f docker-compose.prod.yml ps

echo ""
echo "[prod_start] Next steps:"
echo "   1) Verify backend /health:    curl http://127.0.0.1:8000/health"
echo "   2) Tail logs:                 bash scripts/prod_logs.sh backend"
echo "   3) Stop:                      bash scripts/prod_stop.sh"