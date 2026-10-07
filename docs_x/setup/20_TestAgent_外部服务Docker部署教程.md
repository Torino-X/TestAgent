# TestAgent 外部服务 Docker 部署教程

> **配套文档**
> - [21_TestAgent_env参数详解.md](21_TestAgent_env参数详解.md) — .env 参数详解
> - [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) — 从 Git Clone 到启动完整教程
> - [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) — 常见问题 FAQ
> - [25_TestAgent_数据库迁移与初始化.md](25_TestAgent_数据库迁移与初始化.md) — 数据库迁移
> - [docs_x/01 项目技术方案证据索引](../01_TestAgent_项目技术方案证据索引.md)
> - [docs_x/02 项目总体技术方案 §30-31 部署架构](../02_TestAgent_项目总体技术方案.md)

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`
**适用**: 新开发人员 / SRE / 部署工程师

> 本文档覆盖 **MySQL / PostgreSQL / Redis / Elasticsearch / Qdrant** 5 个外部服务的完整 Docker 部署，含开发 / 测试 / 生产 3 套 compose、健康检查、数据持久化、备份恢复、安全建议。

---

## 1. 文档说明

### 1.1 TestAgent 依赖的外部服务

| # | 服务 | 用途 | 端口（test / dev / prod） |
|---|---|---|---|
| 1 | **MySQL 8.0** | 主业务库（users / conversations / messages / files / agent_tasks / agent_runs / agent_events / artifacts / tool_calls / human_confirmations）| 3307 / 5432（云） / 3306（云） |
| 2 | **PostgreSQL 16** | LangGraph Checkpointer（checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations）| 5433 / 5432 / 不暴露 |
| 3 | **Redis 7** | LiveEventBus pub/sub + InFlightRegistry 跨 worker 锁 | 6380 / 6379 / 不暴露 |
| 4 | **Elasticsearch 8** | Context Engine 索引（dense 检索 + lexical 检索）| 不启动（dev） / 不启动（云） |
| 5 | **Qdrant** | Context Engine dense 向量检索（可选） | 不启动（dev） / 不启动（云） |

**注意**：
- **dev 模式**：你已经有云 MySQL，本地只启动 PG + Redis 两个容器
- **test 模式**：MySQL/Postgres/Redis 三个容器，端口错位（3307/5433/6380）避免与本地已有开发库冲突
- **prod 模式**：5 服务全启动，PG/Redis 不暴露公网（只 internal-only）

### 1.2 适用读者

| 角色 | 关注点 |
|---|---|
| **新开发人员** | 开发环境（docker-compose.dev.yml）启动 PG + Redis |
| **测试工程师** | 测试基建（docker-compose.test.yml）启动 3 容器 |
| **SRE / 部署工程师** | 生产环境（docker-compose.prod.yml）5 服务 + nginx |

---

## 2. 架构总览

```mermaid
flowchart TB
    subgraph L1["dev_3.0 部署拓扑"]
        subgraph L2["开发环境（你电脑）"]
            BE1[Backend :8000<br/>uvicorn --reload]
            FE1[Frontend :5313<br/>npm run dev]
        end
        subgraph L3["本地 Docker（PG + Redis）"]
            PG1[Postgres :5432<br/>LangGraph Checkpointer]
            RD1[Redis :6379<br/>LiveEventBus + InFlight]
        end
        subgraph L4["云服务器 MySQL"]
            MY1[MySQL :3306<br/>业务主库]
        end
        BE1 --> PG1
        BE1 --> RD1
        BE1 --> MY1
        FE1 --> BE1
    end

    subgraph L5["test 部署拓扑（CI / e2e）"]
        subgraph L6["docker-compose.test.yml"]
            MT1[MySQL :3307<br/>testagent_test]
            PT1[Postgres :5433<br/>langgraph_test]
            RT1[Redis :6380]
        end
        BET1[Backend<br/>test backend]
        BET1 --> MT1 & PT1 & RT1
    end

    subgraph L7["prod 部署拓扑（云服务器）"]
        subgraph L8["docker-compose.prod.yml"]
            PP1[Postgres :internal]
            RP1[Redis :internal]
            BP1[Backend :127.0.0.1:8000]
            FP1[Frontend :127.0.0.1:3000]
            NX1[Nginx :80/443]
        end
        MY2[云 MySQL :3306]
        BP1 --> PP1 & RP1 & MY2
        NX1 --> BP1 & FP1
    end
```

---

## 3. 服务详细规格

### 3.1 MySQL 8.0

#### 用途

主业务库，存储所有业务数据：

| 表 | 用途 |
|---|---|
| `users` / `conversations` / `messages` / `files` | 用户 / 会话 / 消息 / 上传文件 |
| `agent_tasks` / `agent_runs` / `agent_events` | 任务 / 运行 / 事件 |
| `artifacts` / `tool_calls` | 产物 / 工具调用 |
| `human_confirmations` | 章节确认 / format_loss 确认 |
| `agent_context_snapshots` | Context Engine 快照 |
| `knowledge_configs` / `image_understanding_configs` | 知识库 / 图像理解配置 |
| `model_configs` / `settings` | 模型 / 全局设置 |
| `conversation_summaries` / `conversation_context_*` | 会话摘要 / 上下文 |

#### 关键参数

| 镜像 | `mysql:8.0` |
|---|---|
| 默认用户 | `root` |
| 默认密码（test） | `testpass` |
| 端口 | 3307（test）/ 3306（dev/prod） |
| 默认数据库 | `testagent_test`（test） / `testagent`（dev/prod） |
| 认证插件 | `mysql_native_password` |
| InnoDB 日志 | 64M（test） |
| 最大连接数 | 200（test） |

#### 健康检查

```bash
docker exec testagent-mysql mysqladmin ping -h localhost -u root -ptestpass
# 返回: mysqld is alive
```

### 3.2 PostgreSQL 16

#### 用途

LangGraph Checkpointer 持久化（4 张核心表）：

| 表 | 用途 |
|---|---|
| `checkpoints` | LangGraph checkpoint 主表（thread_id + checkpoint_id + parent_checkpoint_id）|
| `checkpoint_blobs` | 大字段 blob（存储 channel_values）|
| `checkpoint_writes` | pending writes 暂存 |
| `checkpoint_migrations` | 迁移版本追踪 |

#### 关键参数

| 镜像 | `postgres:16-alpine` |
|---|---|
| 默认用户 | `testagent`（dev）/ `test`（test）/ `langgraph`（prod） |
| 默认密码（test） | `testpass` |
| 端口 | 5433（test）/ 5432（dev） / 不暴露（prod） |
| 默认数据库 | `langgraph` |
| 健康检查 | `pg_isready -U <user> -d <db>` |

#### 健康检查

```bash
docker exec testagent-postgres pg_isready -U testagent -d langgraph
# 返回: accepting connections
```

### 3.3 Redis 7

#### 用途

- **LiveEventBus pub/sub**（跨 worker 实时事件总线）
- **InFlightRegistry 跨 worker 锁**（`SET NX EX <ttl>` 互斥锁）
- **SequenceNumberAllocator**（`INCR seq:agent_events:<task_id>` 单调序号）

#### 关键参数

| 镜像 | `redis:7-alpine` |
|---|---|
| 默认端口 | 6380（test）/ 6379（dev） / 不暴露（prod） |
| 默认密码 | 无（test/dev） / `${REDIS_PASSWORD}`（prod） |
| 内存上限 | 256 MB |
| 淘汰策略 | allkeys-lru |
| 持久化 | AOF（appendonly yes） |

#### 关键 Keyspace

| Key 模式 | 用途 |
|---|---|
| `agent_event:bus:<task_id>` | LiveEventBus 频道（Phase 2.6） |
| `inflight:<task_public_id>` | 跨 worker InFlight 锁（Phase 2.8B） |
| `seq:agent_events:<task_id>` | sequence_no 分配（Phase 2.6） |

#### 健康检查

```bash
docker exec testagent-redis redis-cli ping
# 返回: PONG
```

### 3.4 Elasticsearch 8（Context Engine 可选）

#### 用途

Context Engine dense 检索 + lexical 检索（CE-03 接入）。

#### 关键参数

| 镜像 | `elasticsearch:8.15.x` |
|---|---|
| 默认端口 | 9200（HTTP）/ 9300（transport） |
| 默认内存 | 512 MB（dev） / 4 GB（prod） |
| 默认集群名 | `testagent` |

**注意**：本项目 dev / prod 暂不启动 ES。`requirements.txt` 包含 `elasticsearch>=8.15.0,<9.0.0` 仅作为 CE 可选依赖，缺库时 CE 检索降级不伪造。

### 3.5 Qdrant（Context Engine 可选）

#### 用途

Context Engine dense 向量检索（CE-03 接入）。

#### 关键参数

| 镜像 | `qdrant/qdrant:latest` |
|---|---|
| 默认端口 | 6333（HTTP）/ 6334（gRPC） |

**注意**：本项目 dev / prod 暂不启动 Qdrant。`requirements.txt` 包含 `qdrant-client>=1.13.0,<2.0.0` 仅作为 CE 可选依赖。

---

## 4. 开发环境部署（docker-compose.dev.yml）

### 4.1 架构

```
你电脑（本地）
├── Backend :8000（uvicorn --reload）
├── Frontend :5313（npm run dev）
├── Docker
│   ├── Postgres :5432（LangGraph Checkpointer）
│   └── Redis :6379（LiveEventBus + InFlight）
└── → 云 MySQL :3306（业务主库）
```

### 4.2 启动步骤

#### Step 1：复制环境变量模板

```bash
# 项目根目录
cp .env.test.example backend/.env

# 编辑 backend/.env，修改：
# - DATABASE_URL → 你的云 MySQL 真实地址
# - POSTGRES_PASSWORD → 你想设的 PG 密码
# - REDIS_PASSWORD → 你想设的 Redis 密码（或留空）
```

#### Step 2：设置 docker-compose 环境变量

```bash
# 在项目根目录创建 .env.dev（gitignore）
cat > .env.dev <<'EOF'
POSTGRES_PASSWORD=testpgpass
REDIS_PASSWORD=
EOF
```

#### Step 3：启动 PG + Redis 容器

```bash
docker compose -f docker-compose.dev.yml up -d
# 输出: Creating network "testagent_default" ...
#        Creating testagent-pg-dev    ... done
#        Creating testagent-redis-dev  ... done
```

#### Step 4：验证容器状态

```bash
docker compose -f docker-compose.dev.yml ps
# NAME                  STATUS              PORTS
# testagent-pg-dev       Up (healthy)        0.0.0.0:5432->5432/tcp
# testagent-redis-dev     Up (healthy)        0.0.0.0:6379->5432/tcp
```

#### Step 5：检查 Postgres / Redis 健康

```bash
# Postgres
docker exec testagent-pg-dev pg_isready -U testagent -d langgraph
# 返回: accepting connections

# Redis
docker exec testagent-redis-dev redis-cli ping
# 返回: PONG
```

#### Step 6：本地 Python 连接测试

```bash
cd backend

# Postgres
psql "postgresql+psycopg://testagent:testpgpass@localhost:5432/langgraph" -c "SELECT 1;"
# 返回: 1

# Redis
redis-cli -h localhost -p 6379 ping
# 返回: PONG

# MySQL（云）
mysql -h your.cloud.mysql.host -u youruser -p yourpass testagent -e "SELECT 1;"
# 返回: 1
```

### 4.3 停止

```bash
docker compose -f docker-compose.dev.yml down
# 数据保留（volume: pgdev / redisdev）
# 完全清理（volume 也删）：
docker compose -f docker-compose.dev.yml down -v
```

---

## 5. 测试环境部署（docker-compose.test.yml）

### 5.1 架构

```
CI / 本地 e2e
├── docker-compose.test.yml
│   ├── MySQL :3307（testagent_test）
│   ├── Postgres :5433（langgraph_test）
│   └── Redis :6380
├── Backend（test backend）
└── Pytest e2e tests
```

### 5.2 启动步骤

#### Step 1：使用启动脚本

```bash
# 推荐：使用项目自带脚本（自动等待端口 ready）
bash scripts/phase2_8r_start_test_infra.sh
# 输出: 
#   [phase2_8r_start_test_infra] MySQL port 3307 ready (took 3s)
#   [phase2_8r_start_test_infra] Postgres port 5433 ready (took 2s)
#   [phase2_8r_start_test_infra] Redis port 6380 ready (took 1s)
#   [phase2_8r_start_test_infra] Test infra ready.
```

#### Step 2：手动启动（可选）

```bash
docker compose -f docker-compose.test.yml up -d
# 等 30s
sleep 30
# 验证 MySQL
docker exec testagent-mysql mysqladmin ping -h localhost -u root -ptestpass
# 验证 Postgres
docker exec testagent-postgres pg_isready -U test -d langgraph_test
# 验证 Redis
docker exec testagent-redis redis-cli ping
```

#### Step 3：跑 e2e 测试

```bash
# 一键跑（start infra + e2e）
bash scripts/phase2_8r_run_e2e.sh
```

#### Step 4：停机 + 清理

```bash
# 停机
bash scripts/phase2_8r_stop_test_infra.sh

# 或手动
docker compose -f docker-compose.test.yml down
# 清理（tmpfs → 完全清空）
docker compose -f docker-compose.test.yml down -v
```

### 5.3 tmpfs 数据清理

**关键**：测试环境使用 `tmpfs`（`/var/lib/mysql` 等），容器停止时**自动清空**——这是测试隔离的关键设计。

```yaml
# docker-compose.test.yml 关键片段
services:
  mysql-test:
    tmpfs:
      - /var/lib/mysql  # tmpfs → 容器停即清
  postgres-test:
    tmpfs:
      - /var/lib/postgresql/data  # tmpfs → 容器停即清
  redis-test:
    tmpfs:
      - /data  # tmpfs → 容器停即清
```

---

## 6. 生产环境部署（docker-compose.prod.yml）

### 6.1 架构

```
云服务器
├── 外部已存在
│   └── MySQL :3306（testagent）
├── docker-compose.prod.yml
│   ├── Postgres :internal（langgraph）   ← 不暴露公网
│   ├── Redis :internal                  ← 不暴露公网
│   ├── Backend :127.0.0.1:8000         ← loopback only
│   ├── Frontend :127.0.0.1:3000        ← loopback only
│   └── Nginx :80/443                    ← 对外 HTTPS 入口
└── 数据持久化（named volumes）
    ├── pgdata
    └── redisdata
```

### 6.2 前置要求

| 资源 | 要求 |
|---|---|
| 操作系统 | Ubuntu 22.04 LTS / 20.04 LTS |
| Docker | >= 24.0 |
| Docker Compose | >= v2.0 |
| 内存 | >= 4 GB（prod） / 8 GB（推荐） |
| 磁盘 | >= 50 GB |
| 云 MySQL | 已存在（连 DATABASE_URL 即可） |
| 域名 | 已有（Let's Encrypt cert 申请） |

### 6.3 启动步骤

#### Step 1：拉取项目

```bash
cd /opt
sudo git clone <your-repo-url> testagent
cd testagent
sudo git checkout dev_3.0
```

#### Step 2：创建 .env.prod（必须用 sudo 限制权限）

```bash
# 重要：.env.prod 含真实 secret，必须 600
sudo bash -c 'cat > .env.prod <<EOF
# ── 业务库 MySQL ──
DATABASE_URL=mysql+pymysql://testagent:STRONG_PASS@127.0.0.1:3306/testagent?charset=utf8mb4
MYSQL_ROOT_PASSWORD=STRONG_PASS

# ── LangGraph Checkpointer ──
POSTGRES_PASSWORD=STRONG_PASS

# ── Redis ──
REDIS_PASSWORD=STRONG_PASS

# ── LLM ──
AGENT_RUNTIME_LLM_API_KEY=sk-xxxxxxxxxxxxxx
# AGENT_RUNTIME_LLM_BASE_URL=https://your-llm-gateway.com/v1

# ── 守 #18：必须显式写 ──
AGENT_RUNTIME_CANARY_PERCENT=0
AGENT_RUNTIME_DEFAULT_ENGINE=legacy
EOF
sudo chmod 600 .env.prod
sudo chown root:root .env.prod
```

#### Step 3：启动生产栈

```bash
bash scripts/prod_start.sh
# 内部调用：docker compose -f docker-compose.prod.yml up -d
# 输出：
#   [prod_start] Pulling images...
#   [prod_start] Starting stack...
#   [prod_start] Waiting for Postgres / Redis healthchecks...
#   [prod_start] State:
#   NAME                    STATUS              PORTS
#   testagent-postgres      Up (healthy)        ← 不暴露
#   testagent-redis         Up (healthy)        ← 不暴露
#   testagent-backend       Up (healthy)        127.0.0.1:8000->8000
#   testagent-frontend      Up                  127.0.0.1:3000->3000
#   testagent-nginx         Up                  80, 443
```

#### Step 4：验证健康检查

```bash
# Backend health
curl http://127.0.0.1:8000/health
# 返回：{"status":"ok"}

# 日志关键字确认
bash scripts/prod_logs.sh backend | grep "Phase 2.8A Postgres Checkpointer 已就绪"
bash scripts/prod_logs.sh backend | grep "Phase 2.8R-B AgentExecutionWorker 已就绪"
```

#### Step 5：HTTPS cert 申请（首次）

```bash
# 安装 certbot
sudo apt install certbot python3-certbot-nginx

# 申请 cert
sudo certbot --nginx -d your.domain.com

# 自动续期 cron（certbot install 时已加；手动加）
echo "0 3 * * * certbot renew --quiet" | sudo crontab -
```

#### Step 6：停止

```bash
bash scripts/prod_stop.sh
# 或手动
docker compose -f docker-compose.prod.yml down
```

### 6.4 数据持久化

```yaml
# docker-compose.prod.yml
volumes:
  pgdata:    # Postgres 数据（命名 volume）
    driver: local
  redisdata: # Redis AOF
    driver: local
```

**关键**：
- **MySQL**：用云上已有实例，**不**在 compose 内
- **Postgres**：用 `pgdata` 命名 volume（停容器数据保留）
- **Redis**：用 `redisdata` 命名 volume（AOF 持久化）

### 6.5 守禁令映射

| 守 | 实现 |
|---|---|
| **守 #18** | `AGENT_RUNTIME_CANARY_PERCENT=0` + `AGENT_RUNTIME_DEFAULT_ENGINE=legacy` **必须显式写在 .env.prod**（生产默认不启用 langgraph）|
| **守 #1** | LangGraph 生产必须用 Postgres checkpointer（不静默 fallback MemorySaver）|
| **守 #2** | MySQL 已用云上实例，**不**在 compose 内重复 |
| **守 #5** | MySQL 跟 PG / Redis 不同实例，建议 MySQL 走内网 / SSH tunnel |

---

## 7. 数据持久化与备份

### 7.1 MySQL 备份（云实例）

```bash
# 假设 MySQL 跑在云服务器
mysqldump -h localhost -u root -p testagent > /opt/backups/mysql-$(date +%F).sql

# 压缩
gzip /opt/backups/mysql-$(date +%F).sql

# 保留 30 天
find /opt/backups -name "mysql-*.sql.gz" -mtime +30 -delete
```

### 7.2 Postgres 备份（命名 volume pgdata）

```bash
# 方式 1: pg_dump
docker exec testagent-postgres pg_dump -U langgraph langgraph | gzip > /opt/backups/pg-$(date +%F).sql.gz

# 方式 2: 备份整个 volume
docker run --rm -v testagent_pgdata:/data -v /opt/backups:/backup \
  alpine tar czf /backup/pgdata-$(date +%F).tar.gz /data
```

### 7.3 Redis 备份（命名 volume redisdata）

```bash
# 方式 1: AOF dump
docker exec testagent-redis redis-cli -a "$REDIS_PASSWORD" BGSAVE
# AOF 文件在 /data/appendonly.aof

# 方式 2: 复制 volume
docker run --rm -v testagent_redisdata:/data -v /opt/backups:/backup \
  alpine tar czf /backup/redisdata-$(date +%F).tar.gz /data
```

### 7.4 恢复流程

```bash
# 步骤 1: 停止后端
bash scripts/prod_stop.sh

# 步骤 2: 恢复 Postgres
docker compose -f docker-compose.prod.yml down -v
# 重新启动
bash scripts/prod_start.sh
# 恢复 backup
gunzip < /opt/backups/pg-2026-08-25.sql.gz | docker exec -i testagent-postgres psql -U langgraph langgraph

# 步骤 3: 恢复 Redis
gunzip < /opt/backups/redisdata-2026-08-25.tar.gz | docker exec -i testagent-redis sh -c "tar xzf - -C /data"

# 步骤 4: 验证
bash scripts/prod_logs.sh backend | tail -20
```

---

## 8. 常见配置

### 8.1 端口冲突

| 端口 | 用途 | 冲突时如何处理 |
|---|---|---|
| 3306 / 3307 | MySQL | 改 `docker-compose.test.yml` 中 `3307:3306` |
| 5432 / 5433 | Postgres | 改 `docker-compose.test.yml` 中 `5433:5432` |
| 6379 / 6380 | Redis | 改 `docker-compose.test.yml` 中 `6380:6379` |

**操作**：

```bash
# 编辑 docker-compose.test.yml
vim docker-compose.test.yml

# 重启
docker compose -f docker-compose.test.yml down
docker compose -f docker-compose.test.yml up -d
```

### 8.2 资源限制

```yaml
# docker-compose.prod.yml 中给 backend 加资源限制
services:
  backend:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G
        reservations:
          cpus: '1'
          memory: 2G
```

### 8.3 日志

```bash
# 实时日志
bash scripts/prod_logs.sh backend
bash scripts/prod_logs.sh postgres
bash scripts/prod_logs.sh redis

# 按时间过滤
bash scripts/prod_logs.sh backend --since 1h
```

### 8.4 时区

容器内默认 UTC。生产如果需要北京时间：

```yaml
# docker-compose.prod.yml
services:
  backend:
    environment:
      TZ: Asia/Shanghai
```

---

## 9. 安全建议

### 9.1 端口暴露原则

| 端口 | dev | test | prod |
|---|---|---|---|
| MySQL 3306 | **云上已有**（不走 docker）| 3307（暴露）| **云上已有** |
| Postgres 5432 | 5432（暴露到 0.0.0.0） | 5433（暴露）| **不暴露**（internal-only）|
| Redis 6379 | 6379（暴露到 0.0.0.0）| 6380（暴露）| **不暴露**（internal-only）|
| Backend 8000 | 8000（你电脑本机）| - | 127.0.0.1（loopback）|
| Frontend 3000 | 3000（你电脑本机）| - | 127.0.0.1（loopback）|
| Nginx 80/443 | - | - | 80/443（对外） |

### 9.2 密码管理

| 服务 | dev 密码 | prod 密码 |
|---|---|---|
| MySQL | 云上自己的 | 云上自己的（已有）|
| Postgres | `testpgpass`（明文 .env.dev）| `STRONG_PASS`（.env.prod 600 权限）|
| Redis | 无密码 | `STRONG_PASS`（.env.prod 600 权限）|
| LLM API Key | - | `sk-xxxxx`（SettingsService 注入，**不**入 .env.prod）|

**关键规则**：
- `.env.prod` 必须 `chmod 600` + `chown root:root`
- LLM API Key **不**入 `.env.prod`，走 SettingsService 从 DB 注入

### 9.3 网络隔离

```yaml
# docker-compose.prod.yml 关键
networks:
  app-net:        # 自定义 bridge，PG/Redis/Backend/Frontend 互通
    driver: bridge

# PG/Redis 故意不写 ports: → 不暴露
```

### 9.4 最小化镜像

```bash
# 推荐 alpine 镜像（mysql 用 debian-slim 因为 alpine 兼容性差）
postgres:16-alpine
redis:7-alpine
nginx:1.25-alpine
```

---

## 10. 验证清单

| 验证项 | 命令 | 期望 |
|---|---|---|
| MySQL 连通 | `mysql -h <host> -u <user> -p<pw> -e "SELECT 1;"` | `1` |
| Postgres 健康 | `docker exec testagent-postgres pg_isready -U <user>` | `accepting connections` |
| Redis 健康 | `docker exec testagent-redis redis-cli ping` | `PONG` |
| Backend 健康 | `curl http://127.0.0.1:8000/health` | `{"status":"ok"}` |
| Frontend 可访问 | `curl http://127.0.0.1:3000` | HTML 200 |
| 健康检查端点 | `curl http://127.0.0.1:8000/api/health/db` | `{"db":"ok","postgres":"ok","redis":"ok"}` |
| Nginx 反代 | `curl https://your.domain.com/health` | `{"status":"ok"}` |

---

## 11. 索引自检

- [x] 5 个外部服务清单（MySQL/Postgres/Redis/ES/Qdrant）
- [x] 开发环境部署（docker-compose.dev.yml）
- [x] 测试环境部署（docker-compose.test.yml）
- [x] 生产环境部署（docker-compose.prod.yml）
- [x] 健康检查（MySQL/Postgres/Redis）
- [x] 数据持久化（Postgres/Redis 命名 volume）
- [x] 备份与恢复（mysqldump / pg_dump / redis BGSAVE）
- [x] 常见配置（端口冲突 / 资源限制 / 时区）
- [x] 安全建议（端口暴露原则 / 密码管理 / 网络隔离）
- [x] 验证清单
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

**文档完成。配套阅读：[21_TestAgent_env参数详解.md](21_TestAgent_env参数详解.md) + [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md)。**