#!/usr/bin/env bash
# scripts/dev_check.sh
# 本机 → 云服务器 连接性验证(在本地电脑跑,不是云服务器)

set -e

# ── 配置(改成你的真实值) ──
CLOUD_HOST="${CLOUD_HOST:?set CLOUD_HOST=your.cloud.server.ip}"

echo "=========================================="
echo "  TestAgent 开发环境连接性检查"
echo "=========================================="
echo ""

# 1) Ping(可选,可能 ICMP 被禁)
echo "[1/5] ping $CLOUD_HOST ..."
ping -c 2 -W 2 "$CLOUD_HOST" 2>&1 | tail -3 || echo "  ping 失败(可能 ICMP 被禁,正常)"

# 2) MySQL port
echo ""
echo "[2/5] MySQL port 3306 ..."
timeout 3 bash -c "echo > /dev/tcp/$CLOUD_HOST/3306" 2>/dev/null && echo "  ✓ 端口可达" || echo "  ✗ 端口不可达(检查云安全组)"

# 3) Postgres port
echo ""
echo "[3/5] Postgres port 5432 ..."
timeout 3 bash -c "echo > /dev/tcp/$CLOUD_HOST/5432" 2>/dev/null && echo "  ✓ 端口可达" || echo "  ✗ 端口不可达(检查云安全组)"

# 4) Redis port
echo ""
echo "[4/5] Redis port 6379 ..."
timeout 3 bash -c "echo > /dev/tcp/$CLOUD_HOST/6379" 2>/dev/null && echo "  ✓ 端口可达" || echo "  ✗ 端口不可达(检查云安全组)"

# 5) Python 连接测试(需本地装了 pymysql / psycopg / redis)
echo ""
echo "[5/5] Python 客户端连接测试 ..."
python3 << 'PY' || echo "  python 客户端测试需要 pip install pymysql psycopg-binary redis"
import os
import socket

host = os.environ.get("CLOUD_HOST")
if not host:
    print("  (skip: CLOUD_HOST not set)")
else:
    print(f"  host={host}")
    # MySQL 端口测通就够(实际连接要正确密码)
    for name, port in [("MySQL", 3306), ("Postgres", 5432), ("Redis", 6379)]:
        try:
            with socket.create_connection((host, port), timeout=3):
                print(f"  ✓ {name} :{port}")
        except Exception as e:
            print(f"  ✗ {name} :{port} — {e}")
PY

echo ""
echo "=========================================="
echo "  检查完"
echo "=========================================="
echo ""
echo "所有 ✓ 后,再:"
echo "  cd backend"
echo "  cp .env.test.example .env  # 改 DATABASE_URL 等"
echo "  alembic upgrade head"
echo "  uvicorn app.main:app --reload"