# TestAgent 前端

TestAgent 前端是测试资产智能体平台的浏览器工作台。它基于 Vue 3 构建，通过 HTTP 与 Server-Sent Events（SSE）连接 FastAPI 后端，把当前的测试方案生成流程呈现为可追溯的对话式任务，而不是一次不可控的提示词调用。

> **当前发布范围：** v1.0.0 已端到端交付测试方案生成流程。平台未来会逐步支持更多测试资产，但测试用例生成、缺陷分析报告等能力目前尚未发布，请不要将其视为现有功能。

产品截图、平台定位、许可证与发布边界请参阅[项目根目录 README](../README.md)；数据库、后端服务和 API 启动请参阅[后端 README](../backend/README.md)。

## 技术栈

- Vue 3 + TypeScript
- Vite 6
- Vue Router
- Pinia
- Naive UI 与 Ionicons
- 原生 `fetch` / Axios API 模块与 SSE 事件流处理
- Vitest
- Markdown、Mermaid 与 PDF 预览支持

## 当前可使用的工作区

| 工作区 | 当前作用 |
| --- | --- |
| 对话工作台 | 新建、恢复测试任务；上传需求文档/模板；查看 Agent 事件并处理确认卡片。 |
| 测试方案交付 | 查看任务结果、生成/审查反馈及可下载产物。 |
| 模板市场 | 搜索和筛选模板，上传和管理个人模板，下载/发布符合条件的模板，并在新任务中使用模板。 |
| 资料库 | 上传、搜索、筛选、预览、重命名、下载、软删除和恢复可复用资料。 |
| 项目 | 组织会话、项目资料和测试资产；项目会话将关联的项目资料作为上下文来源。 |
| 设置 | 通过后端配置用户级模型连接信息和可选的公司知识库连接信息。 |
| 帮助中心 | 浏览内置帮助文章。 |

## 前置条件

- 推荐 Node.js 20 LTS 或更高版本。
- npm（随 Node.js 安装）。
- 若要进行真实数据和任务联调，需要先启动 TestAgent 后端；请按[后端开发说明](../backend/README.md)操作。

检查本机环境：

```powershell
node --version
npm --version
```

## 开发环境启动（Windows / PowerShell）

### 1. 安装依赖

```powershell
Set-Location <TestAgent-仓库路径>\frontend
npm ci
```

`npm ci` 会严格按 `package-lock.json` 安装依赖。只有在你有意修改依赖并准备更新锁文件时，才应使用 `npm install`。

### 2. 配置本地后端地址

在 `frontend/` 下创建本地 `.env` 文件。它已被 Git 忽略，不能写入生产凭据。

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8003
```

该端口应与后端启动时选定的端口一致。按[后端 README](../backend/README.md)的 Windows 推荐命令启动后端时，默认使用 `8003`：

```powershell
Set-Location <TestAgent-仓库路径>\backend
.\.venv\Scripts\Activate.ps1
python scripts/run_dev.py --host 127.0.0.1 --port 8003
```

`VITE_API_BASE_URL` 通常应填写不带结尾斜杠的后端源地址，例如 `http://127.0.0.1:8003`。客户端也兼容以 `/api` 结尾的地址，但仅填写源地址更容易理解和排查。

> Vite 会把名称以 `VITE_` 开头的变量暴露给浏览器代码。因此它只能用于公开配置，**不能**放置 LLM Key、数据库密码、对象存储密钥、私有服务令牌或任何其他秘密。模型与公司知识库凭据应由已认证用户通过后端设置接口配置。

### 3. 启动 Vite 开发服务器

```powershell
Set-Location <TestAgent-仓库路径>\frontend
npm run dev
```

Vite 固定监听 <http://127.0.0.1:5318>，并启用了 `--strictPort`。若端口被占用，它会停止而不是悄悄切换端口，这样可以让后端 CORS 配置保持确定。

在浏览器打开 <http://127.0.0.1:5318>，默认路由会跳转到 `/login`。

### 4. 验证后端连通性

排查 UI 前，先确认后端可用：

```powershell
Invoke-WebRequest http://127.0.0.1:8003/api/health | Select-Object -ExpandProperty Content
```

若后端使用其他端口，请同时替换该命令及 `VITE_API_BASE_URL` 中的 `8003`，然后重启 `npm run dev`。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 在 `127.0.0.1:5318` 启动 Vite 开发服务器。 |
| `npm run build` | 使用 `vue-tsc` 做类型检查，并在 `dist/` 生成生产构建。 |
| `npm run preview` | 在 `127.0.0.1:5318` 本地预览已构建的生产包。 |
| `npm test` | 运行一次 Vitest 测试套件。 |

修改前端后，建议执行：

```powershell
npm test
npm run build
```

`dist/`、`node_modules/` 均为本地生成目录，已被 Git 忽略。

## 路由

| 路由 | 页面 |
| --- | --- |
| `/login`、`/register` | 登录与注册入口。 |
| `/chat`、`/chat/:conversationId` | 会话与 Agent 任务工作台。 |
| `/settings` | 模型、能力与可选公司知识库配置。 |
| `/library`、`/library/items/:itemId/preview` | 可复用资料库与文档预览。 |
| `/projects`、`/projects/:projectId` | 项目列表与项目资料工作区。 |
| `/templates`、`/templates/:templateId/preview` | 模板市场与模板预览。 |
| `/help`、`/help/article/:slug` | 内置帮助中心。 |

## 前端目录结构

```text
src/
├── api/          # 类型化 HTTP API 模块、请求与错误处理
├── assets/       # 静态视觉资源
├── components/   # 可复用的工作台、对话、设置和 UI 组件
├── composables/  # 可复用 Vue 行为，包括 SSE 处理
├── mocks/        # 隔离的界面/测试夹具数据
├── router/       # 路由声明与导航配置
├── stores/       # Pinia 状态仓库
├── styles/       # 共享样式
├── types/        # 视图/组件共享的 TypeScript 契约
├── utils/        # 格式化与工具函数
└── views/        # 路由级页面
```

### 接口与事件流

普通 API 请求统一经过 `src/api/request.ts`。它读取 `VITE_API_BASE_URL`，无论配置的基地址是否带有 `/api`，都能正确保留请求路径；登录后会携带 Bearer Token。

任务时间线由 `src/composables/useSse.ts` 处理 Server-Sent Events。它会携带当前认证令牌，并支持事件游标（`Last-Event-ID`）；发生短暂断连后，任务页可利用该游标重连并与持久化事件对齐。除非后端契约同时调整，否则不要单独将其替换成另一套 WebSocket 协议。

## 与后端联调

1. 按照[后端 README](../backend/README.md)启动 MySQL、PostgreSQL 和 Redis。
2. 启动后端，并检查 `http://127.0.0.1:8003/api/health`。
3. 在本地 `frontend/.env` 中设置 `VITE_API_BASE_URL=http://127.0.0.1:8003`。
4. 运行 `npm run dev`。
5. 在应用中注册或登录，再到设置页配置模型地址、密钥和模型名称，然后创建 Agent 生成任务。

即使模型不可用，界面仍可展示大部分导航和空状态；但真实文档生成依赖后端、数据库/存储服务及用户模型配置。可选的公司知识库检索与项目资料检索相互独立：公司连接器未配置时，应显示“不可用/已跳过”，而不是被误报为查询成功。

## 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| 页面能打开，但 API 请求发往 `5318` 并返回 404 | 创建或更新 `frontend/.env`：`VITE_API_BASE_URL=http://127.0.0.1:8003`，然后重启 Vite。未配置时，相对 `/api` 请求会发往前端自身。 |
| 浏览器报 CORS 错误 | 确认后端 `CORS_ORIGINS` 包含 `http://127.0.0.1:5318` 或 `http://localhost:5318`，然后重启后端。 |
| `npm run dev` 提示端口被占用 | 停止占用 `5318` 的进程；若确需改端口，应同时修改 Vite 脚本和后端 CORS 配置。 |
| 模型调用提示未配置 | 登录后在设置页填写有效的模型地址、密钥和模型名；不要把密钥写入前端构建变量。 |
| SSE 任务进度停止更新 | 确认后端仍可访问，检查 `/api/health`，再刷新任务页。客户端会在条件允许时使用最新事件游标重连。 |
| 更新依赖后构建/类型检查失败 | 仅删除本地 `node_modules/`，再执行 `npm ci`（或预期的依赖升级命令），最后运行 `npm run build`。不要提交生成的模块目录。 |

## 贡献者安全要求

- `.env`、`node_modules/`、`dist/`、浏览器导出物和临时预览文件都不得提交。
- 所有以 `VITE_` 开头的变量都有可能进入浏览器构建产物；它们是配置，不是秘密存储。
- 不要在 mock、截图或测试夹具中添加客户文档、内网 URL、真实任务记录、模型密钥或公司知识库凭据。
- 提交 UI 修改前运行生产构建与测试，并让所有用户可见文案准确反映当前公开能力范围。

## 相关文档

- [项目总览](../README.md)
- [后端开发说明](../backend/README.md)
- [技术文档目录](../docs_x/)
- [安全策略](../SECURITY.md)
