# TestAgent 后端

TestAgent 后端是测试资产智能体平台的服务层。它向 Web 前端提供 HTTP 与 SSE 接口，负责用户、项目、会话、模板、资料文件和产物的持久化，并承载测试方案生成所使用的 Agent Runtime 与 Context Engine。

> **当前发布范围：** v1.0.0 已完整交付“受控测试方案生成”能力：解析需求文档与 Word 模板、在配置可用时检索项目资料或组织知识、发现关键缺口时向用户澄清、生成并审查测试方案，最后导出 Word 文档。平台底层能力也为未来的测试资产能力服务，但测试用例生成、缺陷分析报告等能力尚不属于 v1.0.0 的已发布承诺。

产品概览、界面截图、许可证与发布范围请参阅[项目根目录 README](../README.md)；浏览器端开发说明请参阅[前端 README](../frontend/README.md)。

## 后端包含什么

| 模块 | 职责 |
| --- | --- |
| `app/api/v1/` | FastAPI 路由：认证、会话、消息、文件、Agent 任务、产物、设置、资料库、项目、模板、帮助中心和 Context Engine 诊断接口。 |
| `app/agent_runtime/` | 长任务派发、事件持久化/流式推送、LangGraph 编排、检查点与任务恢复控制。 |
| `app/context_engine/` | 上下文来源收集、项目资料索引与检索、上下文选择、压缩、请求负载组装、记忆与审计能力。 |
| `app/services/`、`app/repositories/`、`app/models/`、`app/schemas/` | 业务服务、数据访问、SQLAlchemy 模型和 API 数据契约。 |
| `app/storage/`、`app/integrations/`、`app/llm/`、`app/tools/` | 对象存储、外部服务集成、模型适配器与 Agent 工具。 |
| `alembic/` | MySQL 数据库迁移。 |
| `tests/` | 单元、契约、运行时与集成导向测试。 |

## 运行依赖

本地开发环境将业务数据、Agent 运行时协调与缓存分开管理：

```text
Vue 前端 ──HTTP/SSE──> FastAPI (/api)
                            │
           ┌────────────────┼─────────────────┐
           │                │                 │
         MySQL          PostgreSQL         Redis
       业务数据        LangGraph 检查点   运行时 / 缓存
           │
       OSS 对象存储
  上传文件与生成产物
```

| 依赖 | 用途 | 开发环境建议 |
| --- | --- | --- |
| Python 3.11+ | 运行后端服务 | 本机安装。 |
| MySQL 8+ | 业务数据与 Alembic 迁移 | 使用已有的本地、容器或远程开发实例。`docker-compose.dev.yml` **不会**启动 MySQL。 |
| PostgreSQL 16 | 持久化 LangGraph 检查点 | 可使用仓库提供的开发 Compose 文件启动，或连接独立 PostgreSQL。 |
| Redis | 运行时事件/锁协调与业务缓存 | 开发 Compose 会在 `6379` 与 `6380` 启动两个隔离实例。 |
| 阿里云 OSS | 持久化上传源文件与生成产物 | 真实文件流程前，请配置独立的开发 Bucket。 |
| LLM 服务商 | Agent 内容生成 | 每位用户在设置页配置模型地址、密钥和模型名；仓库不包含任何模型密钥。 |
| Qdrant、Elasticsearch、Embedding/Reranker 服务 | 可选的项目资料检索 / Context Engine RAG | 未准备好这些服务前，请保持相关功能开关关闭。 |

## 开发环境启动（Windows / PowerShell）

以下流程用于启动一个本地开发环境。示例只使用本地占位密码；请使用你自己的值，且绝不要将它们提交到仓库。

### 1. 进入仓库根目录

```powershell
Set-Location <TestAgent-仓库路径>
```

### 2. 启动 PostgreSQL 与 Redis

`docker-compose.dev.yml` 会启动 PostgreSQL 16（`5432`）、运行时 Redis（`6379`）与业务缓存 Redis（`6380`）。它需要从当前 PowerShell 会话或根目录 `.env` 中读取 PostgreSQL 密码，并可选读取 Redis 密码。

```powershell
$env:POSTGRES_PASSWORD = "替换为本地开发密码"
$env:REDIS_PASSWORD = "替换为本地开发密码"

docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
```

后续创建 `backend/.env` 时还需要这些密码。若密码中含有 `@`、`:`、`/`、`#` 等特殊字符，写入连接 URL 前必须进行百分号编码。

停止这组开发容器：

```powershell
docker compose -f docker-compose.dev.yml down
```

### 3. 准备 MySQL

请准备可访问的 MySQL 8+ 开发数据库。MySQL 与 PostgreSQL 分工不同：MySQL 保存业务数据，PostgreSQL 保存 Agent 运行时检查点。若使用全新的本地 MySQL，可按本机数据库管理规范创建数据库，例如：

```sql
CREATE DATABASE testagent CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

下一步会把对应的同步、异步 MySQL 连接地址写入 `backend/.env`。

### 4. 创建后端环境文件

```powershell
Set-Location backend
Copy-Item .env.example .env
```

在本地编辑 `.env`。该文件已被 Git 忽略，**不得提交，也不要把完整内容粘贴到 Issue、日志或聊天中**。至少替换下列占位配置：

```dotenv
# 业务数据库：同一个数据库，使用不同 SQLAlchemy 驱动
DATABASE_SYNC_URL=mysql+pymysql://USER:PASSWORD@127.0.0.1:3306/testagent?charset=utf8mb4
DATABASE_ASYNC_URL=mysql+aiomysql://USER:PASSWORD@127.0.0.1:3306/testagent?charset=utf8mb4

# JWT 签名密钥：请生成一个新的本地值
SECRET_KEY=CHANGE_ME_BEFORE_PRODUCTION

# 第 2 步启动的 Docker 服务
AGENT_RUNTIME_POSTGRES_URL=postgresql+psycopg://testagent:POSTGRES_PASSWORD@127.0.0.1:5432/langgraph?ssl=false
AGENT_RUNTIME_REDIS_URL=redis://:REDIS_PASSWORD@127.0.0.1:6379/0
AGENT_RUNTIME_REDIS_INFLIGHT_URL=redis://:REDIS_PASSWORD@127.0.0.1:6379/1
CACHE_REDIS_URL=redis://:REDIS_PASSWORD@127.0.0.1:6380/0

# 允许 Vite 前端访问后端
CORS_ORIGINS=["http://localhost:5318","http://127.0.0.1:5318"]
```

`SECRET_KEY=CHANGE_ME_BEFORE_PRODUCTION` 只是醒目的占位文本。任何非一次性的环境都必须替换成独有随机值；切勿把生产密钥复用于本地开发。

若要使用完整的文件上传与产物导出流程，还需配置独立开发 Bucket 的 `OSS_ENDPOINT`、`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_BUCKET_NAME`。未配置时，`/api/health/dependencies` 会将持久文件存储显示为 `unconfigured`，不能视为生产就绪。

`.env.example` 中的 Context Engine/RAG、动态 Agent、提示词转储和调试开关均为特性开关。请先保持安全默认值；仅在依赖服务与权限均准备完毕后，逐项启用。

### 5. 创建虚拟环境并安装依赖

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

若 PowerShell 仅阻止当前终端激活虚拟环境，可先执行一次：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

`requirements.txt` 使用了 `sqlalchemy[asyncio]`，它会安装 SQLAlchemy 异步扩展所需的 `greenlet`。

### 6. 验证 MySQL 并执行迁移

```powershell
python scripts/check_db.py
alembic upgrade head
```

迁移只能针对 `.env` 所指向的开发数据库运行。对共享环境升级前，请先备份数据库并由团队审查迁移内容。

### 7. 启动 API

Windows 上建议使用仓库提供的启动器。它会在 Uvicorn 启动前设置异步 PostgreSQL 驱动需要的 Selector 事件循环策略：

```powershell
python scripts/run_dev.py --host 127.0.0.1 --port 8003
```

该启动器在 Windows 上刻意不允许 `--reload`。修改后端代码后，请按 `Ctrl+C` 停止并手动重启。非 Windows 开发环境也可直接运行：

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8003
```

启动后可访问：

| 地址 | 说明 |
| --- | --- |
| `http://127.0.0.1:8003/docs` | 开发环境 Swagger/OpenAPI 页面。 |
| `http://127.0.0.1:8003/redoc` | 开发环境 ReDoc API 文档。 |
| `http://127.0.0.1:8003/api/health` | 服务进程与 Agent Runtime 摘要。 |
| `http://127.0.0.1:8003/api/health/db` | 轻量 MySQL 连通性检查。 |
| `http://127.0.0.1:8003/api/health/dependencies` | 数据库、Redis 与持久存储配置状态。 |
| `http://127.0.0.1:8003/api/health/readiness` | 严格就绪检查；启用的 Worker 或依赖未准备好时可返回 `503`。 |

### 8. 启动前端

请在另一个终端按照[前端 README](../frontend/README.md)操作。若后端运行在 `8003` 端口，前端本地且被忽略的 `.env` 应写为：

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8003
```

## 配置说明

`backend/.env.example` 是唯一权威的公开、无密钥配置参考，按子系统组织：

| 配置组 | 控制内容 | 开发建议 |
| --- | --- | --- |
| `DATABASE_*` | MySQL 连接，供迁移与应用请求使用 | 必填。同步、异步 URL 应指向同一开发数据库。 |
| `SECRET_KEY`、`ACCESS_TOKEN_*`、`CORS_ORIGINS` | 认证签名与浏览器访问控制 | 真实浏览器联调必需。除临时环境外请使用新密钥。 |
| `AGENT_RUNTIME_POSTGRES_*`、`AGENT_RUNTIME_REDIS_*` | 检查点、事件/锁协调与 Worker | 使用本仓库 Compose 服务或等价的隔离实例。 |
| `CACHE_REDIS_*` | 业务缓存后端与缓存保护机制 | 基础探索可选；验证缓存行为时配置 `6380`。 |
| `OSS_*` | 上传原始文件和导出产物的持久存储 | 使用独立开发 Bucket 与最小权限凭据。 |
| `CONTEXT_*`、`QDRANT_*`、`ELASTIC_*`、`EMBEDDING_*`、`RERANKER_*` | Context Engine 索引/检索及可选召回服务 | 仅在对应服务已经就绪时启用。 |
| `LOG_*`、`OTEL_*` | 结构化日志与可选 OpenTelemetry 导出 | 未接入获批 Collector 前保持遥测导出关闭。 |
| `*_DEBUG_*`、`CONTEXT_PROMPT_DUMP_*` | 开发诊断 | 仅在受控本地排障时短暂开启；提示词转储可能含任务敏感上下文。 |

模型配置按用户通过设置页保存；常规运行时不读取旧的全局模型密钥环境变量。可选的公司知识库连接器同样由用户配置为外部服务；本仓库不包含公司知识库地址、密钥或企业资料语料。

## 测试与质量检查

若当前虚拟环境还未安装测试工具：

```powershell
python -m pip install "pytest>=8.3" "pytest-asyncio>=0.24"
```

开发中运行单个测试模块：

```powershell
pytest tests/test_agent_task_detail.py -q
```

仅在已准备好的隔离开发环境中运行完整后端测试：

```powershell
pytest -q
```

`scripts/` 下部分集成与端到端脚本需要外部服务、模型配置或初始化数据。运行前请阅读脚本顶部说明，并使用独立开发账号/数据库；它们并不等价于无依赖单元测试。

## 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| `No module named 'greenlet'` 或 SQLAlchemy asyncio 导入失败 | 确认已激活目标虚拟环境，再执行 `python -m pip install -r requirements.txt`。声明的 `sqlalchemy[asyncio]` 会安装所需依赖。 |
| 启动日志显示 PostgreSQL/Redis 降级 | 执行 `docker compose -f docker-compose.dev.yml ps`，检查 `.env` 中三个运行时 URL，并确认密码特殊字符已编码。 |
| `scripts/check_db.py` 失败 | 检查 MySQL 是否运行、`testagent` 是否存在，以及 `DATABASE_SYNC_URL`、`DATABASE_ASYNC_URL` 的主机、账号和驱动是否正确。 |
| 浏览器报 CORS 错误 | 确保 `CORS_ORIGINS` 包含 `http://127.0.0.1:5318` 或 `http://localhost:5318`，再重启后端。 |
| 前端无法访问 API | 在 `frontend/.env` 设置 `VITE_API_BASE_URL=http://127.0.0.1:8003`，重启 Vite 后检查 `/api/health`。 |
| 文件上传/导出依赖状态为 `unconfigured` | 配置独立开发 OSS Bucket；切勿为图省事接入生产 Bucket 或长期凭据。 |
| `/api/health/readiness` 返回 `503` | 该接口刻意采用严格检查。查看响应中的 `checks`，只启用已准备好依赖的 Worker/RAG 服务。 |

## 贡献者安全要求

- 不得提交 `.env`、虚拟环境、`data/`、`logs/`、`output/`、`tmp/`、提示词转储、生成产物或本地数据库导出文件。
- 文档、测试和夹具必须使用占位值，不能放入 API Key、密码、Bucket 名、客户地址或内网主机名。
- 外部检索、对象存储与遥测服务均应使用最小权限的开发账号。
- 提示词转储、上传文件、生成的 Word 文档和运行日志都可能含敏感信息；即使未触发传统密钥扫描，也应按敏感数据处理。

## 相关文档

- [项目总览](../README.md)
- [前端开发说明](../frontend/README.md)
- [技术文档目录](../docs_x/)
- [安全策略](../SECURITY.md)
