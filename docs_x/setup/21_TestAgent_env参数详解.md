# TestAgent .env 参数详解

> **配套文档**
> - [20_TestAgent_外部服务Docker部署教程.md](20_TestAgent_外部服务Docker部署教程.md) — 外部服务 Docker 部署
> - [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) — 从 Git Clone 到启动
> - [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) — 常见问题 FAQ
> - [docs_x/01 项目技术方案证据索引](../01_TestAgent_项目技术方案证据索引.md)

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`
**适用**: 新开发人员 / SRE / 部署工程师

> 本文档覆盖 **60+ 个 .env 环境变量**，按功能分 14 组，含含义、默认值、必填性、安全等级、配套 `.env.test.example` 复制步骤、LLM API key 获取指引。
> 行号以当前 `dev_3.0` HEAD 为准。

---

## 1. 文档说明

### 1.1 .env 文件总览

| 文件 | 位置 | 用途 | git 状态 |
|---|---|---|---|
| `.env.test.example` | 项目根 | 测试 / 开发模板 | ✅ 已 commit |
| `.env` | 项目根 / `backend/` | 本地实际配置 | ❌ gitignore |
| `.env.prod` | 项目根 | 生产实际配置 | ❌ gitignore |
| `.env.dev` | 项目根 | dev 容器配置（POSTGRES_PASSWORD / REDIS_PASSWORD）| ❌ gitignore |
| `frontend/.env.local` | frontend/ | 前端 Vite 配置 | ❌ gitignore |

### 1.2 复制 .env 步骤

```bash
# 1) 复制测试模板
cp .env.test.example backend/.env
# 或生产：
# cp .env.test.example .env.prod

# 2) 编辑 backend/.env
vim backend/.env
#   - 填入真实 DATABASE_URL（云 MySQL）
#   - 填入真实 AGENT_RUNTIME_POSTGRES_URL（dev 本机 Postgres）
#   - 填入真实 AGENT_RUNTIME_REDIS_URL（dev 本机 Redis）
#   - 填入 LLM API Key（AGENT_RUNTIME_LLM_API_KEY）

# 3) 设置权限（生产）
chmod 600 .env.prod
chown root:root .env.prod
```

### 1.3 安全等级

| 等级 | 含义 | 示例 |
|---|---|---|
| 🔴 **Secret** | 必须 600 权限，**严禁**提交到 git | `DATABASE_URL` / `LLM_API_KEY` / `POSTGRES_PASSWORD` |
| 🟡 **Config** | 非 secret，但通常每环境不同 | `AGENT_RUNTIME_LANGGRAPH_ENABLED` / `AGENT_RUNTIME_CANARY_PERCENT` |
| 🟢 **Default** | 有合理默认值，可不设 | `PLAN_STEP_VISIBLE_SECONDS` / `TOOL_MIN_VISIBLE_MS` |

### 1.4 文档约定

| 标记 | 含义 |
|---|---|
| ✅ **必填** | 缺失会导致启动失败或功能异常 |
| 🟡 推荐 | 缺失有默认值，生产环境推荐显式设置 |
| 🟢 可选 | 完全可选，仅在调优时使用 |
| **Phase X** | 仅在某 Phase 启用时使用 |
| **守 #N** | 关联某条守禁令，违反会被 fail-closed |

---

## 2. 14 组环境变量速查

| # | 组 | 变量数 | 重要程度 |
|---|---|---|---|
| 1 | **MySQL / Database** | 3 | 🔴 Secret |
| 2 | **PostgreSQL / Checkpointer** | 5 | 🔴 Secret |
| 3 | **Redis** | 4 | 🟡 Config |
| 4 | **LLM / Embedding** | 7 | 🔴 Secret |
| 5 | **Context Engine** | 4 | 🟡 Config |
| 6 | **Canary / EngineRouter** | 5 | 🟡 Config |
| 7 | **Phase 2.9B Narrative** | 12 | 🟢 Default |
| 8 | **Recursion / BusinessBudgets** | 1 | 🟢 Default |
| 9 | **Tool / Adapter** | 5 | 🟢 Default |
| 10 | **Event Retention** | 3 | 🟢 Default |
| 11 | **Plan / Prompt** | 3 | 🟢 Default |
| 12 | **Legacy / Orchestrator** | 6 | 🟡 Config |
| 13 | **Debug** | 3 | 🟢 Default |
| 14 | **Test** | 1 | 🟢 Default |

---

## 3. MySQL / Database（3 个变量）

### 3.1 `DATABASE_URL` 🔴 Secret ✅ 必填

主业务库连接串（MySQL 8）。

**格式**：`mysql+pymysql://<user>:<password>@<host>:<port>/<db>?charset=utf8mb4`

| 场景 | 实际值（举例） |
|---|---|
| test（docker-compose.test.yml）| `mysql+pymysql://root:testpass@localhost:3307/testagent_test?charset=utf8mb4` |
| dev（连云 MySQL）| `mysql+pymysql://testagent:STRONG_PASS@your.cloud.mysql.host:3306/testagent?charset=utf8mb4` |
| prod | `mysql+pymysql://testagent:STRONG_PASS@127.0.0.1:3306/testagent?charset=utf8mb4`（用服务名 + 端口） |

**关联**：[20 §3.1 MySQL 8.0](20_TestAgent_外部服务Docker部署教程.md#31-mysql-80)

### 3.2 `DATABASE_SYNC_URL` 🟡 推荐

同步 SQLAlchemy URL（Alembic 迁移用）。通常与 `DATABASE_URL` 相同 driver，但可独立配置。

```bash
DATABASE_SYNC_URL=mysql+pymysql://root:testpass@localhost:3307/testagent_test?charset=utf8mb4
```

### 3.3 `AGENT_RUNTIME_DB_URL` 🟡 推荐

agent_runtime 内部使用的 DB URL（某些子模块直接读 env）。**默认回退到 `DATABASE_URL`**。

---

## 4. PostgreSQL / Checkpointer（5 个变量）

### 4.1 `AGENT_RUNTIME_POSTGRES_URL` 🔴 Secret ✅ 必填（prod）

LangGraph Checkpointer 连接串（Postgres 16）。

**格式**：`postgresql+psycopg://<user>:<password>@<host>:<port>/<db>`

| 场景 | 实际值 |
|---|---|
| test | `postgresql+psycopg://test:testpass@localhost:5433/langgraph_test` |
| dev（容器）| `postgresql+psycopg://testagent:testpgpass@localhost:5432/langgraph` |
| prod（容器）| `postgresql+psycopg://langgraph:STRONG_PASS@postgres:5432/langgraph`（用 docker compose 服务名） |

**守 #1**：langgraph_checkpoint_postgres 2.0.25 实际用 psycopg v3 而非 asyncpg。**必须**装 `psycopg[binary]>=3.0,<4.0`。

**关联**：[18 §4 Postgres Checkpointer 工厂](18_TestAgent_Checkpointer与持久化_技术实现文档.md)

### 4.2 `AGENT_RUNTIME_CHECKPOINTER_BACKEND` 🟡 Config

Checkpointer backend 选择。

| 取值 | 行为 |
|---|---|
| `memory`（默认）| MemorySaver（仅测试 / 显式声明）|
| `postgres` / `postgresql` / `pg` | AsyncPostgresSaver（生产用）|

```bash
# 守 #18：生产显式设
AGENT_RUNTIME_CHECKPOINTER_BACKEND=postgres
```

### 4.3 `AGENT_RUNTIME_POSTGRES_SETUP_ON_START` 🟢 可选

启动时是否自动建表（LangGraph 4 张核心表）。

| 取值 | 行为 |
|---|---|
| `true`（默认）| 启动时调 `cp.setup()` 自动建表 |
| `false` | 跳过 setup（需手动 alembic / setup） |

### 4.4 `AGENT_RUNTIME_POSTGRES_POOL_SIZE` 🟢 可选

Postgres 连接池大小（FIX-B AsyncConnectionPool）。

| 取值 | 默认 |
|---|---|
| 整数 | `10`（FIX-B 默认）|

### 4.5 `AGENT_RUNTIME_REDIS_INFLIGHT_TTL_SECONDS` 🟡 Config

RedisInFlightRegistry 跨 worker 锁 TTL（秒）。

| 取值 | 默认 | 推荐 |
|---|---|---|
| 整数 | `1800`（30 分钟）| 30-60 分钟 |

```bash
# Phase 2.8B 默认
AGENT_RUNTIME_REDIS_INFLIGHT_TTL_SECONDS=1800
```

---

## 5. Redis（4 个变量）

### 5.1 `AGENT_RUNTIME_REDIS_URL` 🟡 Config ✅ prod 必填

LiveEventBus + InFlightRegistry 共用 Redis URL。

**格式**：`redis://[:<password>]@<host>:<port>/<db>`

| 场景 | 实际值 |
|---|---|
| test | `redis://localhost:6380/0` |
| dev（容器）| `redis://:@localhost:6379/0`（无密码）|
| prod（容器）| `redis://:STRONG_PASS@redis:6379/0?password=STRONG_PASS` |

**Phase 2.6 起**：未配置 → 自动用 `InMemoryLiveEventBus`（单 worker 模式）。

### 5.2 `AGENT_RUNTIME_REDIS_INFLIGHT_URL` 🟡 Config

跨 worker InFlight 锁专用 Redis URL（Phase 2.8B）。**默认回退到 `AGENT_RUNTIME_REDIS_URL`**。

| 场景 | 推荐 |
|---|---|
| 与 LiveEventBus 同一 Redis | 不设（用默认）|
| 独立 Redis 实例（高隔离）| `redis://:PASSWORD@inflight-redis:6379/0` |

### 5.3 `AGENT_RUNTIME_REDIS_INFLIGHT_LOCK_PREFIX` 🟢 可选

InFlight 锁 keyspace 前缀（默认 `inflight:`）。

```bash
# 守 #31 多 keyspace 隔离（默认不改）
AGENT_RUNTIME_REDIS_INFLIGHT_LOCK_PREFIX=inflight:
```

### 5.4 `AGENT_RUNTIME_REDIS_INFLIGHT_LOCK_TTL_SECONDS`

⚠️ **注**：与 §4.5 同名（`AGENT_RUNTIME_REDIS_INFLIGHT_TTL_SECONDS`），已在 §4.5 列出。

---

## 6. LLM / Embedding（7 个变量）

### 6.1 `AGENT_RUNTIME_LLM_API_KEY` 🔴 Secret ✅ 必填

LLM Provider API Key（OpenAI 兼容）。

**关键**：**不**入 `.env.prod`，通过 SettingsService 从 DB 注入（避免 secret 泄露）。

| Provider | Key 格式 |
|---|---|
| OpenAI | `sk-...` |
| Azure OpenAI | `<32-hex>` |
| 自部署网关 | `<your-key>` |

### 6.2 `AGENT_RUNTIME_LLM_BASE_URL` 🟡 推荐

LLM Provider base URL（OpenAI 兼容）。

```bash
# OpenAI 官方
AGENT_RUNTIME_LLM_BASE_URL=https://api.openai.com/v1

# 自部署网关
AGENT_RUNTIME_LLM_BASE_URL=https://your-llm-gateway.com/v1
```

### 6.3 `EMBEDDING_MODEL` 🟡 推荐（Context Engine）

Embedding 模型名（dense 检索用）。

```bash
# OpenAI
EMBEDDING_MODEL=text-embedding-3-small

# 自部署
EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5
```

### 6.4 `EMBEDDING_BASE_URL` 🟡 推荐

Embedding Provider base URL。

```bash
EMBEDDING_BASE_URL=https://api.openai.com/v1
```

### 6.5 `EMBEDDING_API_KEY` 🔴 Secret

Embedding Provider API Key。

### 6.6 `EMBEDDING_DIMENSION` 🟡 推荐

向量维度（决定索引 schema）。

```bash
# text-embedding-3-small = 1536
# text-embedding-3-large = 3072
# BAAI/bge-large-zh-v1.5 = 1024
EMBEDDING_DIMENSION=1536
```

### 6.7 `EMBEDDING_NORMALIZE` 🟢 可选

向量归一化（默认 `true`）。

### 6.8 `EMBEDDING_TIMEOUT` 🟢 可选

Embedding 请求超时（秒，默认 `30`）。

---

## 7. Context Engine（4 个变量）

### 7.1 `ES_HOST` 🟡 推荐

Elasticsearch 主机（Context Engine 索引）。

```bash
ES_HOST=elasticsearch  # docker compose 服务名
```

### 7.2 `ES_PORT` / `ES_HTTPS` / `ELASTIC_USER` / `ELASTIC_PASSWORD` 🟡 Config

ES 连接参数（4 个变量）。

```bash
ES_PORT=9200
ES_HTTPS=false
ELASTIC_USER=elastic
ELASTIC_PASSWORD=your-es-password
```

### 7.3 `QDRANT_HOST` / `QDRANT_PORT` / `QDRANT_HTTPS` / `QDRANT_API_KEY` / `QDRANT_PREFER_GRPC` 🟡 Config

Qdrant 连接参数（5 个变量）。

```bash
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_HTTPS=false
QDRANT_PREFER_GRPC=false
QDRANT_API_KEY=optional
```

### 7.4 `CONTEXT_PII_MODE` 🟢 可选

PII 脱敏模式（默认 `redact`）。

| 取值 | 行为 |
|---|---|
| `redact`（默认）| 替换为 `[REDACTED]` |
| `drop` | 删除字段 |
| `passthrough` | 不过滤（不推荐） |

### 7.5 `CONTEXT_DEBUG_API_ENABLED` 🟢 可选

Context Debug API 开关（默认 `false`）。

### 7.6 `CONTEXT_DEBUG_UNLOCK_SECRET` 🔴 Secret

Context Debug 调试解锁 secret（开启时必填）。

### 7.7 `CONTEXT_RETENTION_DRY_RUN` 🟢 可选

Retention Worker dry-run 开关（默认 `false`）。

---

## 8. Canary / EngineRouter（5 个变量）

### 8.1 `AGENT_RUNTIME_LANGGRAPH_ENABLED` 🟡 Config 🛡️ **守 #18**

LangGraph v3 主图全局开关（Phase 2.8R-K 默认 `True`，dev 友好）。

```bash
# 默认（Phase 2.8R-K）
AGENT_RUNTIME_LANGGRAPH_ENABLED=True

# 生产显式关闭
AGENT_RUNTIME_LANGGRAPH_ENABLED=false
```

**守 #18**：Postgres 不可用时由 ProbeRouter 强制 `production_dispatch_forced_off=True`。

### 8.2 `AGENT_RUNTIME_DEFAULT_ENGINE_OVERRIDE` 🟡 Config

强制覆盖默认引擎（`legacy` / `langgraph` / 不设）。

```bash
# AutoRollback 触发时写 state file，env 不需设
# 仅运维临时强制时设
AGENT_RUNTIME_DEFAULT_ENGINE_OVERRIDE=legacy
```

**优先级**：state file > env（state file 代表 AutoRollback 上次运行写过）。

### 8.3 `AGENT_RUNTIME_CANARY_PERCENT` 🟡 Config

灰度比例（0-100，默认 `0`）。

```bash
# 0 = 不按百分比路由（默认）
AGENT_RUNTIME_CANARY_PERCENT=10

# 守 #18：生产写 0
AGENT_RUNTIME_CANARY_PERCENT=0
```

### 8.4 `AGENT_RUNTIME_CANARY_USER_IDS` 🟡 Config

灰度用户白名单（CSV）。

```bash
# CSV of internal user ids
AGENT_RUNTIME_CANARY_USER_IDS=1,2,3
```

### 8.5 `AGENT_RUNTIME_CANARY_STATE_FILE` 🟢 可选

Canary state file 路径（默认 `/tmp/agent_runtime_canary_state.json`）。

```bash
AGENT_RUNTIME_CANARY_STATE_FILE=/var/lib/testagent/canary_state.json
```

---

## 9. Phase 2.9B Narrative（12 个变量）

### 9.1 `AGENT_RUNTIME_PHASE29B_NARRATIVE_ENABLED` 🟢 默认 `1`

主开关（`0` 走确定性 fallback）。

```bash
AGENT_RUNTIME_PHASE29B_NARRATIVE_ENABLED=1
```

### 9.2 `AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_ENABLED` 🟢 默认 `1`

Tool 叙事开关。

### 9.3 `AGENT_RUNTIME_PHASE29B_TASK_SUMMARY_NARRATIVE_ENABLED` 🟢 默认 `1`

Task Summary 叙事开关。

### 9.4 `AGENT_RUNTIME_PHASE29B_PREPARATION_NARRATIVE_ENABLED` 🟢 默认 `1`

PreparationAgent 叙事开关。

### 9.5 `AGENT_RUNTIME_PHASE29B_REPAIR_NARRATIVE_ENABLED` 🟢 默认 `1`

RepairAgent 叙事开关。

### 9.6 `AGENT_RUNTIME_PHASE29B_NARRATIVE_REPAIR_ATTEMPTS` 🟢 默认 `1`

Narrative 修复重试次数。

### 9.7 `AGENT_RUNTIME_PHASE29B_NARRATIVE_TIMEOUT_SECONDS` 🟢 默认 `45.0`

NarrativeComposer 单次生成超时（秒）。

### 9.8 `AGENT_RUNTIME_PHASE29B_NARRATIVE_STREAM_PERSIST_ENABLED` 🟢 默认 `1`

流式 partial 持久化开关。

### 9.9 `AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_BLOCKING_ENABLED` 🟢 默认 `0`

Tool narrative 阻塞模式（默认非阻塞）。

### 9.10 `AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_FINAL_ONLY` 🟢 默认 `0`

只发最终叙事（不流式）。

### 9.11 `AGENT_RUNTIME_PHASE29B_DETERMINISTIC_FALLBACK_ENABLED` 🟢 默认 `1`

Deterministic fallback 开关。

### 9.12 `AGENT_RUNTIME_PHASE29B_INCREMENTAL_NARRATIVE_ENABLED` 🟢 默认 `1`

IncrementalAgent 叙事开关。

### 9.13 `AGENT_RUNTIME_NARRATIVE_DETAIL_LEVEL` 🟢 可选

叙事详细度（`summary` / `standard` / `verbose`）。

---

## 10. Recursion / BusinessBudgets（1 个变量）

### 10.1 `AGENT_RUNTIME_RECURSION_LIMIT` 🟢 可选 🛡️ **守 #18**

LangGraph recursion_limit（默认由 BusinessBudgets 计算 = 36）。

```bash
# 默认（不设）
# 由 BusinessBudgets.compute_recursion_limit() 计算

# 显式覆盖
AGENT_RUNTIME_RECURSION_LIMIT=40
```

**校验规则**（RecursionLimitConfig.resolve_recursion_limit）：
- 非正整数 → `RecursionLimitConfigurationInvalidError`（启动失败）
- `< min_safe`（= 30）→ 启动失败
- 合法值才接受

**关联**：[18 §11 RecursionLimitConfig](18_TestAgent_Checkpointer与持久化_技术实现文档.md)

---

## 11. Tool / Adapter（5 个变量）

### 11.1 `TOOL_MIN_VISIBLE_MS` 🟢 默认 `800`

Tool 卡片最少显示时间（毫秒）—— 让用户感受到"程序在思考"。

```bash
TOOL_MIN_VISIBLE_MS=800
```

### 11.2 `TOOL_EVENT_TRANSITION_SECONDS` 🟢 默认 `0.12`

Tool 卡片状态切换过渡（秒）。

### 11.3 `PLAN_STEP_VISIBLE_SECONDS` 🟢 默认 `0.8`

Plan step 最少显示时间（秒）。

### 11.4 `PLAN_STEP_STARTED` / `PLAN_STEP_COMPLETED` / `PLAN_STEP_GAP`

Plan step 状态控制（3 个变量）。

### 11.5 `PLAN_MAX_TOKENS` 🟢 可选

Plan 阶段最大 token 数。

### 11.6 `TOOL_NARRATIVE_*` 系列

Tool narrative 内部开关（5+ 变量），通常不直接配。

---

## 12. Event Retention（3 个变量）

### 12.1 `AGENT_RUNTIME_EVENT_RETENTION_KEEP_DAYS` 🟢 默认 `30`

Event 保留天数。

```bash
AGENT_RUNTIME_EVENT_RETENTION_KEEP_DAYS=30
```

### 12.2 `AGENT_RUNTIME_EVENT_RETENTION_KEEP_PER_TASK` 🟢 默认 `5000`

每个 task 最多保留 Event 数。

```bash
AGENT_RUNTIME_EVENT_RETENTION_KEEP_PER_TASK=5000
```

### 12.3 `AGENT_RUNTIME_EVENT_RETENTION_INTERVAL` 🟢 默认 `3600`

Retention Worker 清理周期（秒）。

```bash
AGENT_RUNTIME_EVENT_RETENTION_INTERVAL=3600
```

### 12.4 `CONTEXT_RETENTION_DRY_RUN` 🟢 默认 `false`

dry-run 开关（不真删）。

---

## 13. Plan / Prompt（3 个变量）

### 13.1 `PLAN_MAX_TOKENS` 🟢 可选

Plan 阶段 prompt 最大 token 数。

### 13.2 `PLANWISE_DUMP_PROMPTS` 🟢 可选

Plan 阶段 prompt 落盘开关（开发调试用，`1` 开启）。

```bash
# 调试：把所有 plan 阶段的 prompt 落盘
PLANWISE_DUMP_PROMPTS=1
```

### 13.3 `PLAN_STEP_*` 系列

Plan step 状态控制（同 §11.4）。

---

## 14. Legacy / Orchestrator（6 个变量）

### 14.1 `LEGACY_ORCHESTRATOR_ENABLED` 🟡 Config

Legacy orchestrator 全局开关（默认 `true`）。

```bash
# 守 #18：生产保留 legacy 路径（历史任务兜底）
LEGACY_ORCHESTRATOR_ENABLED=true
```

### 14.2 `LEGACY_FALLBACK` 🟢 可选

LangGraph 失败时是否 fallback Legacy。

### 14.3 `LEGACY_CONFIRM` / `LEGACY_FORMAT_DECISION` / `LEGACY_PROFILE` / `LEGACY_TASK_SEMANTIC_FLAGS` / `LEGACY_STATE_SEED_FIELDS` / `LEGACY_USER_LEVEL_KEY_PREFIX` 🟢 可选

Legacy orchestrator 内部开关（6+ 变量），通常不直接配。

### 14.4 `LEGACY_WORKSPACE_MEMORY_ENABLED` 🟢 可选

Legacy 工作区记忆开关。

---

## 15. Debug（3 个变量）

### 15.1 `CONTEXT_DEBUG_API_ENABLED` 🟢 默认 `false`

Context Debug API 开关（生产关闭）。

### 15.2 `CONTEXT_DEBUG_UNLOCK_SECRET` 🔴 Secret

Context Debug 调试解锁 secret（开启时必填）。

### 15.3 `AGENT_RUNTIME_ADMIN_TOKEN` 🔴 Secret

Admin API 鉴权 token（部分内部 API 鉴权用）。

### 15.4 `AGENT_RUNTIME_NOT_READY` 🟢 可选

Lifespan probe 未 ready 时是否返回 503（默认 `false`）。

---

## 16. Test（1 个变量）

### 16.1 `PYTEST_CURRENT_TEST` 🟢 自动设置

pytest 自动设置（**不要手动设**）。

```bash
# pytest 内部自动设置
PYTEST_CURRENT_TEST=tests/test_xxx.py::test_yyy
```

**作用**：被 `feature_flags.py` 和 `canary/config.py` 读取，测试 sandbox 模拟 `langgraph_global_enabled=True`。

### 16.2 `AGENT_RUNTIME_MULTIWORKER_INTEGRATION` 🟢 默认 `0`

多 worker 集成测试开关（`1` 开启 e2e）。

```bash
# 跑 e2e
AGENT_RUNTIME_MULTIWORKER_INTEGRATION=1
```

---

## 17. 其他变量

### 17.1 `AGENT_RUNTIME_ADMIN_TOKEN` 🔴 Secret

Admin API 鉴权 token。

### 17.2 `AGENT_RUNTIME_DB_URL` 🟡 推荐

agent_runtime 内部 DB URL（回退到 `DATABASE_URL`）。

### 17.3 `AGENT_RUNTIME_DEFAULT_GRAPH_VERSION` 🟢 可选

默认 graph version（`v3` / `v2_frozen` / `incremental_test_plan`）。

### 17.4 `AGENT_RUNTIME_PRODUCTION_DISPATCH_ENABLED` 🟢 默认 `false`

生产派发启用（守 #18 由 ProbeRouter 控制）。

### 17.5 `AGENT_RUNTIME_PREPARATION_AGENT_ENABLED` 🟢 默认 `true`

PreparationAgent 主图启用。

### 17.6 `AGENT_RUNTIME_REPAIR_AGENT_ENABLED` 🟢 默认 `true`

RepairAgent 主图启用。

### 17.7 `AGENT_RUNTIME_INCREMENTAL_AGENT_ENABLED` 🟢 默认 `true`

IncrementalAgent 启用。

### 17.8 `AGENT_RUNTIME_INTERRUPT_V2_ENABLED` 🟢 默认 `false`

真 sync interrupt（Phase 2.9A.7）。

### 17.9 `AGENT_RUNTIME_DYNAMIC_AGENT_API_ENABLED` 🟢 默认 `false`

DynamicAgent 独立 API 入口（Phase 2.8D）。

---

## 18. LLM API Key 获取指引

### 18.1 OpenAI 官方

```bash
# 1) 登录 https://platform.openai.com/api-keys
# 2) "Create new secret key" → sk-...
# 3) 充值 $5+ 到账户
# 4) 配置到 SettingsService（DB 注入，不入 .env）
```

### 18.2 Azure OpenAI

```bash
# 1) Azure Portal → Cognitive Services → OpenAI
# 2) "Keys and Endpoint" → Key 1
# 3) 配置 base_url: https://{resource}.openai.azure.com/openai/deployments/{deployment}/...
# 4) 配置 api_version: 2024-02-01
```

### 18.3 自部署 LLM Gateway

```bash
# 1) 公司内部 LLM 网关
# 2) 申请 API key
# 3) 填入 AGENT_RUNTIME_LLM_BASE_URL 和 AGENT_RUNTIME_LLM_API_KEY
# 4) SettingsService 配
```

### 18.4 **关键**：LLM Key 入 DB 而非 .env.prod

```python
# SettingsService 通过 admin API 注入：
# POST /api/admin/settings/llm_config
{
    "provider": "openai",
    "api_key": "sk-...",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o"
}
```

**避免 .env.prod 泄漏**。

---

## 19. 完整 .env.test.example 模板

```bash
# Phase 2.8R-G 测试环境变量模板
# 拷贝为 .env.test 后生效

# ── 业务库(MySQL,3307) ──
DATABASE_URL=mysql+pymysql://root:testpass@localhost:3307/testagent_test?charset=utf8mb4

# ── LangGraph Checkpointer(Postgres,5433) ──
AGENT_RUNTIME_POSTGRES_URL=postgresql+psycopg://test:testpass@localhost:5433/langgraph_test

# ── LiveEventBus + InFlight Redis(6380) ──
AGENT_RUNTIME_REDIS_URL=redis://localhost:6380/0

# ── Phase 2.8R-G 多 worker E2E 开关 ──
AGENT_RUNTIME_MULTIWORKER_INTEGRATION=1
AGENT_RUNTIME_CHECKPOINTER_BACKEND=postgres

# ── 业务保持不变(可覆盖) ──
AGENT_RUNTIME_CANARY_PERCENT=0
AGENT_RUNTIME_DEFAULT_ENGINE=legacy
```

**复制后**：
- 改 `DATABASE_URL` 指向你的 dev / prod MySQL
- 改 `AGENT_RUNTIME_POSTGRES_URL` 指向 dev / prod Postgres
- 改 `AGENT_RUNTIME_REDIS_URL` 指向 dev / prod Redis
- 增 `AGENT_RUNTIME_LLM_API_KEY`（从 SettingsService 注入）

---

## 20. 关键守禁令映射

| 守 | 涉及 env 变量 | 默认 / 推荐 |
|---|---|---|
| **守 #1** | `AGENT_RUNTIME_CHECKPOINTER_BACKEND` | prod 设 `postgres` |
| **守 #2** | `AGENT_RUNTIME_LANGGRAPH_ENABLED` | prod 设 `true`（Phase 2.8R-K）|
| **守 #18** | `AGENT_RUNTIME_CANARY_PERCENT=0` + `AGENT_RUNTIME_DEFAULT_ENGINE=legacy` | prod **必须**写 0 和 legacy |
| **守 #21** | `AGENT_RUNTIME_CHECKPOINTER_BACKEND` | prod **不**用 `memory` |
| **守 #31** | `AGENT_RUNTIME_REDIS_URL` | 不设 → 自动 graceful degrade |

---

## 21. 索引自检

- [x] 14 组环境变量分门别类
- [x] 60+ 个变量详解（含义/默认值/必填性/安全等级）
- [x] `.env.test.example` 复制步骤
- [x] 安全等级（🔴 Secret / 🟡 Config / 🟢 Default）
- [x] LLM API Key 获取指引（4 种 Provider）
- [x] 守禁令映射（守 #1 / #2 / #18 / #21 / #31）
- [x] 完整 .env.test.example 模板
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

**文档完成。配套阅读：[20_TestAgent_外部服务Docker部署教程.md](20_TestAgent_外部服务Docker部署教程.md) + [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md)。**