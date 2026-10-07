# TestAgent 依赖与版本管理

> **配套文档**
> - [20_TestAgent_外部服务Docker部署教程.md](20_TestAgent_外部服务Docker部署教程.md) — 外部服务 Docker 部署
> - [21_TestAgent_env参数详解.md](21_TestAgent_env参数详解.md) — .env 参数详解
> - [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) — 从 Git Clone 到启动
> - [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md) — 常见问题 FAQ
> - [24_TestAgent_依赖与版本管理.md](24_TestAgent_依赖与版本管理.md) — 本文档

**编制时间**: 2026-08-25
**对应分支**: `dev_3.0`

> 本文档覆盖 **Python / Node / Docker / OS / 关键包**的版本约束、升级策略、兼容性矩阵。接手人最常因为版本问题踩坑，这份是必读。
> 行号以当前 `dev_3.0` HEAD 为准。

---

## 1. 文档说明

### 1.1 版本管理的关键问题

TestAgent 的依赖有 **3 类严格锁定**：

| 类型 | 锁法 | 原因 |
|---|---|---|
| **精确锁定（==）** | `langgraph==0.3.34` / `langgraph-checkpoint-postgres==2.0.25` | LangGraph 0.3.x API 不向后兼容；LangGraph checkpointer 接口固定 |
| **下限（>=）** | `fastapi>=0.115.0` / `sqlalchemy>=2.0.36` | 接受 patch + minor 升级 |
| **范围（>=,<）** | `redis>=5.0.0,<6.0.0` / `psycopg[binary]>=3.0,<4.0` | 跨大版本不兼容 |

### 1.2 升级成本

| 升级类型 | 风险 | 验证时间 |
|---|---|---|
| patch（0.1.0 → 0.1.1）| 🟢 低 | 跑后端单测 |
| minor（0.3.x → 0.4.x）| 🟡 中 | 跑单测 + LangGraph 集成测试 |
| major（0.3 → 1.0）| 🔴 高 | 全部 + e2e |

---

## 2. 系统级依赖

### 2.1 Python

| 项 | 最低 | 推荐 |
|---|---|---|
| Python | 3.11+ | 3.12 |
| pip | 23+ | 最新 |
| venv | 内置 | pyenv 装 3.12 |

**关键约束**（来自 `backend/pyproject.toml`）：
```toml
requires-python = ">=3.11"
```

**3.11 必须**：因为用到 `typing.Self` / `tomllib` / `asyncio.timeout` 等 3.11 特性。

### 2.2 Node.js

| 项 | 最低 | 推荐 |
|---|---|---|
| Node.js | 20+ | 22 LTS |
| npm | 10+ | 10+（随 Node） |

**关键约束**（来自 `frontend/package.json`）：`package.json` 没设 `engines.node`，但：
- **Vite 6.x** 需要 Node 18+
- **Vue 3.5** 需要 Node 16+
- **vitest 2.x** 需要 Node 18+

**建议**：`nvm install 22 && nvm use 22`（参考 [22 §2.2](22_TestAgent_从Git到启动完整教程.md)）

### 2.3 Docker

| 项 | 最低 | 推荐 |
|---|---|---|
| Docker Engine | 24.0+ | 最新 |
| Docker Compose | v2.0+ | 最新 |
| 内存（dev）| 4 GB | 8 GB |
| 内存（prod）| 4 GB | 8 GB |

**关键约束**：所有 docker-compose.yml 用 `version: "3.9"`，需要 Docker Compose v2+。

### 2.4 操作系统

| OS | 支持 | 备注 |
|---|---|---|
| macOS 13+ | ✅ | 推荐（Apple Silicon） |
| Ubuntu 22.04 LTS+ | ✅ | 生产推荐 |
| Windows 11 WSL2 | ✅ | 必走 WSL2 |
| Windows 原生 | ⚠️ | ProactorEventLoop 需切到 Selector |
| CentOS 7 | ❌ | Python 3.11 装不上 |
| Alpine | ⚠️ | PaddlePaddle 兼容性差 |

### 2.5 系统库

| 库 | 用途 | macOS | Ubuntu |
|---|---|---|---|
| build-essential | 编译 psycopg 等 | `xcode-select --install` | `apt install build-essential` |
| libssl-dev | Python cryptography | 系统自带 | `apt install libssl-dev` |
| libffi-dev | Python cffi | 系统自带 | `apt install libffi-dev` |
| mysql-client | 调试 MySQL | `brew install mysql-client` | `apt install mysql-client` |
| postgresql-client | 调试 Postgres | `brew install postgresql@16` | `apt install postgresql-client-16` |
| redis-tools | 调试 Redis | `brew install redis` | `apt install redis-tools` |

---

## 3. Python 依赖详解

### 3.1 核心框架（必须）

| 包 | 版本约束 | 锁法 | 原因 |
|---|---|---|---|
| `fastapi` | `>=0.115.0` | 下限 | Pydantic 2.x + lifespan async |
| `uvicorn[standard]` | `>=0.32.0` | 下限 | 支持 websockets / httptools |
| `pydantic` | `>=2.10.0` | 下限 | v2 API + ConfigDict |
| `pydantic-settings` | `>=2.6.0` | 下限 | pydantic-settings v2 |
| `sqlalchemy` | `>=2.0.36` | 下限 | v2 异步 API |
| `alembic` | `>=1.14.0` | 下限 | 兼容 SQLAlchemy 2.x |
| `pymysql` | `>=1.1.1` | 下限 | MySQL driver |
| `aiomysql` | `>=0.2.0` | 下限 | 异步 MySQL |
| `httpx` | `>=0.28.0` | 下限 | 测试用 async client |

### 3.2 LangGraph（**精确锁定**）

| 包 | 版本 | 锁法 | 原因 |
|---|---|---|---|
| `langgraph` | `==0.3.34` | **精确** | 0.3.x API 频繁变更；升级需重跑 Phase 2.0 + 2.1 verification matrix |
| `langchain-core` | `==0.3.86` | **精确** | 与 langgraph 0.3.34 配套 |
| `langgraph-checkpoint-postgres` | `==2.0.25` | **精确** | Postgres checkpointer 接口固定 |
| `psycopg[binary]` | `>=3.0,<4.0` | 范围 | v3 二进制驱动 |

**关键警告**（来自 `requirements.txt` 注释）：
> Phase 2.0 LangGraph runtime - exact floor pins. Bumping requires re-running the Phase 2.0 + 2.1 verification matrix.

### 3.3 Redis

| 包 | 版本约束 | 原因 |
|---|---|---|
| `redis` | `>=5.0.0,<6.0.0` | v5 async API；v6 breaking changes |
| `fakeredis` | `>=2.20.0,<3.0.0` | 测试用；不在主依赖，仅 dev |

### 3.4 Postgres async driver

| 包 | 版本约束 | 原因 |
|---|---|---|
| `asyncpg` | `>=0.29.0,<1.0.0` | langgraph-checkpoint-postgres 2.0.25 实际用 psycopg v3 而非 asyncpg，但 postgresql+asyncpg URL 仍被接受 |

**关键警告**（来自 `requirements.txt`）：
> langgraph-checkpoint-postgres 2.0.25 actually uses psycopg v3 internally (psycopg.AsyncConnection), NOT asyncpg — the postgresql+asyncpg:// URL is accepted but the real driver is psycopg.

### 3.5 PaddlePaddle / PaddleOCR

| 包 | 版本约束 | 原因 |
|---|---|---|
| `paddleocr` | `>=3.0,<4.0` | PaddleOCR 3.x |
| `paddlepaddle` | `>=3.0,<3.3` | PaddlePaddle 3.0-3.2 |

**警告**：Apple Silicon (M1/M2) 装 paddlepaddle 需要：
```bash
pip install paddlepaddle -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3.6 Context Engine 可选依赖（**已纳入主 requirements**）

| 包 | 版本约束 | 用途 |
|---|---|---|
| `qdrant-client` | `>=1.13.0,<2.0.0` | CE dense 向量检索 |
| `elasticsearch` | `>=8.15.0,<9.0.0` | CE 全文检索 |

**关键注释**（来自 `requirements.txt`）：
> CE-03 Context Engine Internal RAG 外部检索通道（dense + lexical）。之前是可选依赖（requirements-ce03-e2e.txt）；Context Engine 完整功能需要它们，纳入主依赖。缺库时检索降级不伪造（Qdrant/ES client 可选 import）。

**仍有 `requirements-ce03-e2e.txt`**：作为快速安装入口 / 版本约束单一来源。

### 3.7 认证 / 工具

| 包 | 版本约束 | 用途 |
|---|---|---|
| `python-jose[cryptography]` | `>=3.3.0` | JWT |
| `passlib[bcrypt]` | `>=1.7.4` | 密码哈希 |
| `python-multipart` | `>=0.0.18` | 文件上传 |
| `python-docx` | `>=1.1.2` | docx 解析 |
| `python-dotenv` | `>=1.0.0` | .env 加载 |

### 3.8 测试

| 包 | 版本约束 | 用途 |
|---|---|---|
| `pytest` | `>=8.3.0` | dev 依赖（pyproject.toml） |
| `pytest-asyncio` | `>=0.24.0` | dev 依赖 |
| `fakeredis` | `>=2.20.0,<3.0.0` | 跨 worker InFlight 测试 |

---

## 4. Node 依赖详解

### 4.1 运行时依赖

| 包 | 版本约束 | 锁法 | 原因 |
|---|---|---|---|
| `vue` | `^3.5.13` | caret（minor + patch 自动）| 3.5.x 稳定 |
| `vue-router` | `^4.5.0` | caret | 4.x |
| `pinia` | `^2.3.0` | caret | 2.x（3.x 是 Vue 3.5 的可选） |
| `naive-ui` | `2.41.0` | **精确** | 2.41.0 是项目验证版本 |
| `@vicons/ionicons5` | `^0.13.0` | caret | 图标库 |
| `axios` | `^1.7.9` | caret | HTTP 客户端 |

**关键警告**：`naive-ui` 锁精确版本（`2.41.0`）。升级到 2.42+ 可能破坏 UI。

### 4.2 开发依赖

| 包 | 版本约束 | 用途 |
|---|---|---|
| `vite` | `^6.0.5` | 构建工具 |
| `vitest` | `^2.1.8` | 单元测试 |
| `vue-tsc` | `^2.2.0` | TypeScript 类型检查 |
| `typescript` | `^5.7.2` | TypeScript |
| `@vitejs/plugin-vue` | `^5.2.1` | Vue 插件 |

---

## 5. 兼容性矩阵

### 5.1 Python / LangGraph 兼容性

| Python | langgraph 0.3.34 | SQLAlchemy 2.0.36 | Pydantic 2.10 |
|---|---|---|---|
| 3.11 | ✅ | ✅ | ✅ |
| 3.12 | ✅ | ✅ | ✅ |
| 3.13 | ⚠️ 未测 | ⚠️ 未测 | ✅ |
| 3.10 | ❌ | ❌ | ❌ |

### 5.2 Postgres / Checkpointer 兼容性

| Postgres | langgraph-checkpoint-postgres 2.0.25 | 备注 |
|---|---|---|
| 16 | ✅ | 生产推荐 |
| 15 | ✅ | 兼容 |
| 14 | ⚠️ 未测 | - |
| 13 | ❌ | 最低 13，但 setup() 表 schema 可能不兼容 |

### 5.3 Redis 兼容性

| Redis | redis-py 5.x | LiveEventBus | 备注 |
|---|---|---|---|
| 7 | ✅ | ✅ | 生产推荐 |
| 6 | ⚠️ | ⚠️ | redis-py 5.x 不支持 |
| 5 | ⚠️ | ⚠️ | 部分功能 |

### 5.4 MySQL 兼容性

| MySQL | SQLAlchemy 2.0 | PyMySQL | 备注 |
|---|---|---|---|
| 8.0 | ✅ | ✅ | 推荐 |
| 5.7 | ✅ | ✅ | 兼容 |
| 5.6 | ⚠️ | ✅ | - |

---

## 6. 升级策略

### 6.1 patch 升级（0.0.x）

```bash
# 例：fastapi 0.115.0 → 0.115.5
# 1) 改 requirements.txt
# 2) 升级 + 测
cd backend
source .venv/bin/activate
pip install --upgrade fastapi
pytest tests/ -x -q
```

### 6.2 minor 升级（0.x.0）

```bash
# 例：langgraph 0.3.34 → 0.4.0
# 1) 必看 CHANGELOG
# 2) 跑全部测试 + e2e
# 3) 验证 Phase 2.0 + 2.1 verification matrix（守 #1）
```

### 6.3 major 升级（x.0.0）

```bash
# 例：langgraph 0.3 → 1.0
# 1) 读 migration guide
# 2) 改 requirements.txt 精确锁定（==x.y.z）
# 3) 跑全部测试
# 4) 部署 staging 跑 1 周
# 5) 升级 production（建议双轨：langgraph v1 + legacy 双写）
```

### 6.4 升级 checklist

```bash
# 1. 备份
cp backend/requirements.txt backend/requirements.txt.bak
cp frontend/package.json frontend/package.json.bak

# 2. 改版本
# 编辑文件

# 3. 重装
cd backend
pip install -r requirements.txt
cd ../frontend
npm install

# 4. 跑后端测试
cd ../backend
pytest tests/ -x -q

# 5. 跑前端测试
cd ../frontend
npm run test

# 6. 跑 e2e（如果涉及 LangGraph）
bash scripts/phase2_8r_run_e2e.sh

# 7. 看 ProbeReport（如果涉及 Postgres / Redis）
# 启动后端，看 banner：
# ProbeReport: postgres_ok=True eventbus_kind=Redis redis_inflight_ok=True
# langgraph_readiness=True checkpointer_type=AsyncPostgresSaver
```

---

## 7. 锁文件管理

### 7.1 `backend/requirements.txt`

**当前锁法**（精确 + 范围 + 下限）：

```text
# 精确锁定（==）
langgraph==0.3.34
langchain-core==0.3.86
langgraph-checkpoint-postgres==2.0.25

# 范围（>=, <）
redis>=5.0.0,<6.0.0
asyncpg>=0.29.0,<1.0.0
psycopg[binary]>=3.0,<4.0
fakeredis>=2.20.0,<3.0.0
paddleocr>=3.0,<4.0
paddlepaddle>=3.0,<3.3
qdrant-client>=1.13.0,<2.0.0
elasticsearch>=8.15.0,<9.0.0

# 下限（>=）
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
pydantic>=2.10.0
...（其余）
```

**为什么不全精确锁定**：
- fastapi 接受 patch 升级（无 breaking change）
- 精确锁定 60+ 包会导致升级困难
- 关键包（langgraph）才精确锁定

### 7.2 `frontend/package.json`

**当前锁法**（多数用 caret）：

```json
{
  "dependencies": {
    "vue": "^3.5.13",
    "naive-ui": "2.41.0",   // ← 精确
    ...
  }
}
```

`^3.5.13` 表示 `>=3.5.13 <4.0.0`（caret 自动锁 minor）。

### 7.3 `requirements-ce03-e2e.txt`

```text
# CE-03 真实外部 E2E 依赖（Qdrant + Elasticsearch 客户端）
# 已纳入主 requirements.txt,本文件保留为快速安装入口 / 版本约束单一来源
qdrant-client>=1.13.0,<2.0.0
elasticsearch>=8.15.0,<9.0.0
```

---

## 8. 关键约束来源

### 8.1 守禁令 → 依赖映射

| 守 | 涉及依赖 |
|---|---|
| **守 #1** | `langgraph-checkpoint-postgres==2.0.25`（生产必须 Postgres）|
| **守 #2** | `langgraph==0.3.34`（默认引擎是 langgraph）|
| **守 #5** | `psycopg[binary]>=3.0,<4.0`（langgraph checkpointer 实际驱动）|
| **守 #18** | `langgraph-checkpoint-postgres==2.0.25`（probe fail 不静默 fallback）|
| **守 #21** | 同 #1（生产不 downgrade MemorySaver）|
| **守 #31** | `redis>=5.0.0,<6.0.0`（优雅降级 InMemory）|

### 8.2 关键注释

`requirements.txt` 关键注释：

```python
# Phase 2.0 LangGraph runtime - exact floor pins.
# Bumping requires re-running the Phase 2.0 + 2.1 verification matrix.
langgraph==0.3.34

# Phase 2.6 Multi-Worker runtime: optional Redis pub/sub for cross-worker
# Live Event Bus. Default off (InMemoryLiveEventBus); opt-in via
# AGENT_RUNTIME_REDIS_URL env. Floor pinned; patch versions OK.
redis>=5.0.0,<6.0.0

# Phase 2.8A: Postgres async driver for LangGraph checkpoint persistence.
# langgraph-checkpoint-postgres (already installed in venv) requires
# asyncpg; without it the AsyncPostgresSaver.from_conn_string() call
# raises ImportError at lifespan probe time.
asyncpg>=0.29.0,<1.0.0

# Phase 2.8B: langgraph-checkpoint-postgres 2.0.25 actually uses
# ``psycopg`` v3 internally (``psycopg.AsyncConnection``), NOT asyncpg
psycopg[binary]>=3.0,<4.0
```

---

## 9. 常见踩坑

### 9.1 `ImportError: paddleocr`（M1 Mac）

**症状**：paddlepaddle 在 Apple Silicon 上没有 wheel

**解决**：
```bash
pip install paddlepaddle -i https://pypi.tuna.tsinghua.edu.cn/simple
# 或用 conda
conda install paddlepaddle
```

### 9.2 `psycopg.OperationalError` 但 Postgres 实际可连

**症状**：psycopg 3 vs 2 API 不兼容，URL 格式不同

**原因**：`postgresql+asyncpg://...` 用 asyncpg，但 `langgraph-checkpoint-postgres 2.0.25` 实际用 psycopg v3

**解决**：用 `postgresql+psycopg://...`（不是 `+asyncpg`）：

```bash
# ✅ 正确
AGENT_RUNTIME_POSTGRES_URL=postgresql+psycopg://user:pass@host:5432/db

# ❌ 错误（asyncpg 装不对会 ImportError）
AGENT_RUNTIME_POSTGRES_URL=postgresql+asyncpg://user:pass@host:5432/db
```

### 9.3 `RecursionLimit` 撞 25

**症状**：
```
RecursionLimit of 25 reached
```

**解决**：提高 `AGENT_RUNTIME_RECURSION_LIMIT` 到 40（参考 [18 §11](18_TestAgent_Checkpointer与持久化_技术实现文档.md)）

### 9.4 Windows ProactorEventLoop

**症状**：
```
RuntimeError: psycopg cannot run in ProactorEventLoop
```

**解决**：
```bash
# 项目 main.py 头部已自动切换
# 但某些场景需显式设：
set EVENT_LOOP_POLICY=WindowsSelectorEventLoopPolicy
# 或用项目脚本
cd backend
python scripts/run_dev.py
```

### 9.5 langgraph-checkpoint-postgres 版本冲突

**症状**：
```
ImportError: cannot import name 'AsyncPostgresSaver' from 'langgraph.checkpoint.postgres.aio'
```

**原因**：langgraph-checkpoint-postgres 2.0.25 锁定，langgraph 0.3.34 需要匹配

**解决**：保持精确锁定：
```bash
pip install --force-reinstall langgraph==0.3.34 langgraph-checkpoint-postgres==2.0.25
```

### 9.6 naive-ui 升级破坏 UI

**症状**：升级 naive-ui 后样式 / API 变化

**解决**：
- **不**随意升 `naive-ui`（锁 2.41.0）
- 必须升时：跑 `npm run test` + 手动验证 11 个 UI 页面（[22 §8.4](22_TestAgent_从Git到启动完整教程.md)）

---

## 10. 升级路径建议

### 10.1 6 个月内可做

| 升级 | 类型 | 建议 |
|---|---|---|
| `fastapi` 0.115 → 0.116 | patch | ✅ 安全，跑单测 |
| `sqlalchemy` 2.0.36 → 2.0.40 | patch | ✅ 安全 |
| `redis` 5.0.x → 5.1.x | minor | ✅ 安全 |
| `elasticsearch` 8.15 → 8.16 | minor | ⚠️ 测一下索引 |

### 10.2 6-12 个月观察

| 升级 | 类型 | 建议 |
|---|---|---|
| `langgraph` 0.3.x → 0.4.x | minor | ⚠️ 需重跑 Phase 2.0 + 2.1 verification |
| `langgraph-checkpoint-postgres` 2.0 → 2.1 | minor | ⚠️ 需重测 cross_worker_recovery |
| `naive-ui` 2.41 → 2.42 | patch | ⚠️ 测 11 个 UI 页面 |

### 10.3 长期

| 升级 | 类型 | 建议 |
|---|---|---|
| `langgraph` 0.3 → 1.0 | **major** | 🔴 需充分测试 + 双轨（langgraph v1 + legacy）|
| Python 3.11 → 3.13 | minor | 🟡 验证 paddlepaddle 兼容性 |
| Node 20 → 22 LTS | minor | 🟡 验证 Vite 6 + vitest 2 |
| Docker Compose v2 → v3 | major | 🔴 重新生成所有 compose 文件 |

---

## 11. 索引自检

- [x] 系统级依赖（Python 3.11+ / Node 20+ / Docker 24+ / OS）
- [x] Python 依赖详解（核心 / LangGraph / Redis / Postgres / PaddlePaddle / CE / 认证 / 测试）
- [x] Node 依赖详解（运行时 / 开发）
- [x] 兼容性矩阵（Python / Postgres / Redis / MySQL）
- [x] 升级策略（patch / minor / major / checklist）
- [x] 锁文件管理（requirements.txt / package.json / requirements-ce03-e2e.txt）
- [x] 关键约束来源（守禁令 → 依赖映射）
- [x] 6 类常见踩坑（paddleocr M1 / psycopg URL / RecursionLimit / ProactorEventLoop / langgraph-checkpoint-postgres / naive-ui）
- [x] 升级路径建议（6 个月内 / 6-12 个月 / 长期）
- [x] 文档中**不包含** codex / claude code / 指导 AI 开发的人员 等描述

**文档完成。配套阅读：[20_TestAgent_外部服务Docker部署教程.md](20_TestAgent_外部服务Docker部署教程.md) + [22_TestAgent_从Git到启动完整教程.md](22_TestAgent_从Git到启动完整教程.md) + [23_TestAgent_常见问题FAQ与故障排查.md](23_TestAgent_常见问题FAQ与故障排查.md)。**