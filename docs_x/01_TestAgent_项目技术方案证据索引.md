# TestAgent 项目技术方案证据索引（dev_3.0）

> **配套文档**
> - 总体技术方案：[docs_x/02_TestAgent_项目总体技术方案.md](docs_x/02_TestAgent_项目总体技术方案.md)
> - 源码导读：[docs_x/03_TestAgent_项目实现原理与源码导读.md](docs_x/03_TestAgent_项目实现原理与源码导读.md)
> - 接手状态：[HANDO.md](HANDO.md)（2026-08-24，最新修复状态）
> - 项目入口：[README.md](README.md)

**编制时间**：2026-08-24
**对应分支**：`dev_3.0`

> 本索引**不重复** 02/03 的内容，只提供：
> 1. 按 docs_x/02/03 章节的**快速跳转**
> 2. 关键架构不变量的**证据位置**（含验证命令）
> 3. 接手时最常查的**关键文件速查**
>
> ⚠️ 行号会随代码变动。验证命令失败时再用 Read/grep 重新定位。

---

## 1. 章节跳转表

### 1.1 docs_x/02 项目总体技术方案（35 章）

| 我想了解 | 章节 |
|---|---|
| 文档说明 / 适用读者 / 当前阶段 | §1 文档说明 |
| 项目定位 / 产品方向 | §2 项目背景与产品定位 |
| 已实现能力清单 | §3 项目核心能力 |
| 总体架构（C4 风格）| §4 总体架构（C4 风格）|
| 技术栈 | §5 技术栈 |
| 前端架构 | §6 前端架构 |
| 后端分层架构 | §7 后端分层架构 |
| Agent 总体架构 | §8 Agent 总体架构 |
| LangGraph v3 主图（30+ 节点）| §9 LangGraph v3 主图 |
| Preparation Agent 子图 | §10 Preparation Agent |
| Review Repair Agent 子图 | §11 Review Repair Agent |
| Incremental Agent 子图 | §12 Incremental Agent |
| 工具系统 | §13 工具系统 |
| 模型调用设计 | §14 模型调用设计 |
| State 与 Runtime Context | §15 State 与 Runtime Context |
| Checkpoint 与服务恢复 | §16 Checkpoint 与服务恢复 |
| 人机协同（Interrupt）| §17 人机协同 |
| 任务调度 | §18 任务调度 |
| Legacy 与 LangGraph 双引擎 | §19 Legacy 与 LangGraph 双引擎 |
| 重试体系 | §20 重试体系 |
| Artifact 与文件幂等 | §21 Artifact 与文件幂等 |
| AgentEvent 系统 | §22 AgentEvent 系统 |
| SSE 与断线恢复 | §23 SSE 与断线恢复 |
| 叙事层（Narrative Composer）| §24 Agent 叙事层 |
| 数据库设计 | §25 数据库设计 |
| 安全设计 | §26 安全设计 |
| 多 Worker 与分布式 | §27 多 Worker 与分布式设计 |
| 可观测性 | §28 可观测性 |
| 测试体系 | §29 测试体系 |
| 部署架构 | §30 部署架构 |
| 完整任务生命周期 | §31 一次完整任务的生命周期 |
| 异常场景 | §32 异常场景 |
| 扩展新 Agent | §33 扩展新 Agent 的方法 |
| **架构不变量（20+ 条）**| **§34 架构不变量** |
| 当前限制 | §35 当前限制与后续规划 |

### 1.2 docs_x/03 项目实现原理与源码导读（23 章）

| 我想了解 | 章节 |
|---|---|
| 阅读方法（10 步顺序）| §1 阅读方法 |
| 目录总览（含全模块树）| §2 项目目录总览 |
| 前端源码阅读路线 | §3 前端源码阅读路线 |
| 后端启动源码路线 | §4 后端启动源码路线 |
| API 到 Service（5 个关键接口）| §5 API 到 Service 路线 |
| Agent 任务创建源码路线 | §6 Agent 任务创建源码路线 |
| LangGraph 运行源码路线 | §7 LangGraph 运行源码路线 |
| Graph v3 源码路线 | §8 Graph v3 源码路线 |
| Preparation 源码（KnowledgeSearch 例）| §9 Preparation 源码路线 |
| Repair 源码（ReviewIssue 修复例）| §10 Repair 源码路线 |
| Incremental 源码（修改单章节例）| §11 Incremental 源码路线 |
| Tool 源码路线 | §12 Tool 源码路线 |
| WordExport 源码 | §13 WordExport 源码路线 |
| Checkpoint 源码 | §14 Checkpoint 源码路线 |
| Event 源码 | §15 Event 源码路线 |
| SSE 源码 | §16 SSE 源码路线 |
| Narrative Composer 源码 | §17 Narrative Composer 源码路线 |
| 数据库源码 | §18 数据库源码路线 |
| Retry 源码 | §19 Retry 源码路线 |
| 取消和恢复源码 | §20 取消和恢复源码路线 |
| 测试源码 | §21 测试源码路线 |
| 常见修改任务导航 | §22 常见修改任务导航 |
| **七天阅读计划** | **§23 新开发人员七天阅读计划** |

---

## 2. 关键架构不变量（dev_3.0）

> 改动前必须确认的不变量。触及这些规则需用户确认。

| # | 规则 | 证据位置 | 验证命令 |
|---|---|---|---|
| 1 | LangGraph 任务不能静默回退 Legacy | `backend/app/agent_runtime/api_dispatcher.py` `_resolve_engine()` | `grep "_resolve_engine"` |
| 2 | 生产不能使用 MemorySaver | `backend/app/agent_runtime/persistence/postgres_checkpointer.py` | `grep "MemorySaver"` |
| 3 | thread_id 不能为空 | `backend/app/agent_runtime/graph_thread_id.py` | – |
| 4 | Graph 版本不可覆盖（v2_frozen 只读）| `backend/app/agent_runtime/graphs/test_plan/versions/v2_frozen/` | `ls -la versions/` |
| 5 | Artifact 必须幂等 | `backend/app/models/artifact.py` `idempotency_key UNIQUE` | – |
| 6 | Event 必须先持久化再 Publish | `backend/app/agent_runtime/event_publisher.py` `publish_with_sink` | `grep "publish_with_sink"` |
| 7 | Tool 输出必须经 Schema 校验 | `backend/app/tools/executor.py` `output_schema.model_validate` | – |
| 8 | 文件必须 atomic rename | `backend/app/services/atomic_file_writer.py` `os.replace` | – |
| 9 | 跨 Worker 任务必须 SKIP LOCKED | `backend/app/repositories/agent_execution_request_repository.py` | – |
| 10 | EngineRouter 决定 engine 后不可切换 | `backend/app/agent_runtime/api_dispatcher.py` `_resolve_engine` | – |
| 11 | PG probe fail 必须 fail-fast | `backend/app/agent_runtime/persistence/probe_router.py` | – |
| 12 | Tool Adapter 白名单外不允许调用 | `backend/app/agent_runtime/adapters/test_agent_tool_adapter.py` | – |
| 13 | Narrative Composer 接管主路径叙事（PublicExecutionUpdateBuilder 仍保留为 fallback）| `backend/app/agent_runtime/narrative_composer/composer.py` + `backend/app/agent/public_execution_update_builder.py` | `ls backend/app/agent_runtime/narrative_composer/` |
| 14 | ResultReviewTool 必须拦截 Word 回填契约问题 | `backend/app/tools/result_review_tool.py` `backfill_contract` | `grep "backfill_contract"` |
| 15 | TestPlanRegenTool 成功后必须再过 ResultReviewTool 复审 | `backend/app/agent_runtime/repair/agent_loop.py` | – |
| 16 | `section_1` 等 UI 临时 ID 必须映射到 `template_structure.generation_config.ai_fields` | `backend/app/tools/result_review_tool.py` `_canonicalize_ai_targets` + `backend/app/tools/test_plan_regen_tool.py` `_config_alias_groups_from_request` | – |
| 17 | 故障注入只应在初次生成后、ResultReview 前构造一次（业务代码只调 gateway）| `backend/app/dev_fault_injection/` + 业务代码调用 `backend/app/fault_injection_gateway.py` | – |
| 18 | 故障注入生产环境必须关闭 | `backend/app/dev_fault_injection/` + `.env` `APP_ENV=production/prod` | – |
| 19 | v3 默认 `interrupt_enabled=True`（真 sync interrupt 节点）；legacy sentinel pause 默认不再走 | `backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_interrupts.py` | – |
| 20 | LLM thinking 不写入 State / Event / UI | `backend/app/agent_runtime/narrative_composer/composer.py`（NarrativeComposer 不读 LLM thinking）| – |

→ 完整 20+ 条不变量见 [docs_x/02 §34](docs_x/02_TestAgent_项目总体技术方案.md)

---

## 3. 关键文件速查

### 3.1 入口与配置
- `backend/app/main.py` — FastAPI lifespan + 配置加载
- `backend/app/api/v1/` — REST 路由（auth/users/conversations/messages/files/agent_tasks/artifacts/...）
- `backend/.env` — 环境变量（**gitignore**，不进仓库；含 MySQL / Postgres / Redis 凭证）
- `.env.test.example` — 测试环境模板

### 3.2 LangGraph v3 主图（11 个节点文件 / 30+ 逻辑节点）
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/graph.py` — 主图装配
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/routing.py` — 边条件
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/routing_after_interrupt.py` — Interrupt 后路由
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/entry_routing.py` — 入口路由
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_pre_confirm.py` — confirm 前节点
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_post_confirm.py` — confirm 后节点
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_review_format.py` — 审查/格式节点
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_narrative.py` — 叙事节点
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_interrupts.py` — Interrupt 节点
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_terminal.py` — 终态节点
- `backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_util.py` — 工具节点

### 3.3 三个动态子图
- `backend/app/agent_runtime/preparation/` — PreparationAgent（10 个文件：agent_loop / subgraph / capabilities / schemas / budget / prompt / permission / tool_filter / event_emitter / fallback）
- `backend/app/agent_runtime/repair/` — RepairAgent（12 个文件：agent_loop / subgraph / capabilities / schemas / budget / prompt / permission / event_emitter / fallback / scope_guard / decision_filter / issue_parser）
- `backend/app/agent_runtime/incremental/` — IncrementalAgent（12 个文件：agent_loop / subgraph / capabilities / schemas / budget / prompt / permission / event_emitter / fallback / scope_guard / decision_filter / artifact_chain）

### 3.4 Context Engine 3.0（22 子模块）
- `backend/app/context_engine/runtime/context_engine.py` — Facade 入口
- `backend/app/context_engine/profiles/registry.py` — Profile Registry + Resolver
- `backend/app/context_engine/sources/` — 10+ Source Adapter（conversation / memory / summary / system_rules / artifact / file_document / knowledge / task_state / workspace_instruction / orchestrator / deadline）
- `backend/app/context_engine/composer/` — Composer + Preflight
- `backend/app/context_engine/retrieval/` — Retrieval + Audit
- `backend/app/context_engine/scope/` — Scope + Task Scope Validator
- `backend/app/context_engine/indexing/` — Lexical + Vector Store + Index Worker
- 其他 14 子模块：`memory/` `snapshot/` `freeze/` `planning/` `security/` `maintenance/` `providers/` `shadow/` `selection/` `tool_output/` `payload/` `compression/` `debug/` `models/` `adapters/`

### 3.5 Narrative Composer
- `backend/app/agent_runtime/narrative_composer/composer.py` — LLM-first 叙事生成 + 1 schema 修复 + 1 fact 修复 + 1 attempt 修复
- `backend/app/agent_runtime/narrative_composer/validator.py` — Schema + 事实约束（数字白名单 / 文件名 / 状态语义 / 敏感信息）
- `backend/app/agent_runtime/narrative_composer/stream_decoder.py` — Tagged Narrative Stream V1
- `backend/app/agent_runtime/narrative_composer/schemas.py` — Pydantic models
- `backend/app/agent_runtime/narrative_composer/context_builders.py` — per-Tool 事实压缩
- `backend/app/agent_runtime/narrative_composer/prompts.py` — prompt 模板

### 3.6 Dynamic Agent（业务实现 + 图装配）
- `backend/app/agent_runtime/dynamic_agent/` — 业务实现（planner / executor / verifier / replanner / synthesizer / plan_validator / tool_handlers / completion_projector / budgets / schemas）
- `backend/app/agent_runtime/graphs/dynamic_agent/graph.py` — LangGraph 装配（34KB，11 节点主图）

### 3.7 Tools（9 个核心工具 + 4 个基础设施 + 1 个迁移路由）
- `backend/app/tools/requirement_parser_tool.py` — 需求解析
- `backend/app/tools/template_parser_tool.py` — 模板解析
- `backend/app/tools/knowledge_search_tool.py` — 知识库检索（Context Engine Maas）
- `backend/app/tools/section_suggestion_tool.py` — 章节建议（F022 user constraint）
- `backend/app/tools/test_plan_generator_tool.py` — 测试方案生成（含 MIG_GENERATE 路径恢复 + ContextInvokerBridge）
- `backend/app/tools/test_plan_regen_tool.py` — 局部重生成（含 cfg_subset self-healing + multi-key 索引 + length 校验 + `_config_alias_groups_from_request` 反查）
- `backend/app/tools/result_review_tool.py` — 自动审查（含 `backfill_contract` 模板回填审查 + `_canonicalize_ai_targets` + 区分工具执行失败与语义失败）
- `backend/app/tools/word_export_tool.py` — Word 导出（tmp + atomic rename）
- `backend/app/tools/docx_format_check_tool.py` — 格式检查
- 基础设施：`base.py` / `executor.py` / `register.py` / `registry.py` / `_mig_routing.py`

### 3.8 Checkpointer / Persistence
- `backend/app/agent_runtime/persistence/postgres_checkpointer.py` — Postgres Checkpointer + `normalize_postgres_conn_string`
- `backend/app/agent_runtime/persistence/probe_router.py` — 探活 + 双闸门（probe fail 双闸门 lockout）
- `backend/app/agent_runtime/recursion_limit_config.py` — 递归限制
- `backend/app/agent_runtime/business_budgets.py` — 业务预算

### 3.9 SSE / Events
- `backend/app/agent_runtime/events/sequence_allocator.py` — Redis INCR 序列分配
- `backend/app/agent_runtime/events/live_agent_event_sink.py` — Live Event Sink
- `backend/app/agent_runtime/events/live_event_bus.py` — Live Event Bus（InMemory / Redis 实现）
- `backend/app/agent_runtime/event_publisher.py` — `publish_with_sink` 双写
- `backend/app/agent_runtime/sse/history_drainer.py` — SSE 启动时历史回放
- `backend/app/api/v1/agent_tasks.py` `stream_events()` — SSE 端点

### 3.10 前端核心
- `frontend/src/components/chat/ChatWorkspace.vue` — 聊天工作台
- `frontend/src/components/cards/AgentRunCard.vue` — 任务运行卡（2k+ 行）
- `frontend/src/components/cards/PublicExecutionUpdate.vue` — 叙事更新组件
- `frontend/src/components/cards/ToolCallMessage.vue` — 工具调用消息
- `frontend/src/composables/useTaskEvents.ts` — 事件映射（24→11 eventType→messageType）
- `frontend/src/composables/useSse.ts` — SSE 连接
- `frontend/src/composables/useTaskEventReducer.ts` — 任务事件 reducer（统一 hydrate/live）
- `frontend/src/stores/chatStore.ts` — 主 store
- `frontend/src/utils/conversationTimeline.ts` — Timeline Assembler（结构化排序）
- `frontend/src/utils/taskState.ts` — TaskRunState derive
- `frontend/src/utils/taskTiming.ts` — 任务时间权威 helper

### 3.11 数据库
- `backend/app/models/` — ORM 模型（artifact / agent_event / human_confirmation / agent_run / agent_task 等）
- `backend/alembic/versions/` — 迁移脚本
- `backend/tests/conftest.py` — 测试 DB（内存 SQLite，Windows EventLoop 强制 `WindowsSelectorEventLoopPolicy`）

### 3.12 Legacy / Atomic Capability / 故障注入
- `backend/app/agent/orchestrator.py` — Legacy orchestrator（保留作对照 + 历史任务兜底，~65KB）
- `backend/app/agent/atomic_capability_registry.py` — Atomic Capability Registry
- `backend/app/agent/public_execution_update.py` — `PublicExecutionUpdate` dataclass
- `backend/app/agent/public_execution_update_builder.py` — 旧确定性 builder（保留为 fallback，主路径由 NarrativeComposer 取代）
- `backend/app/test_faults/__init__.py` — 5 种 dev 环境故障开关（生产环境二次保护关闭）
- `backend/app/dev_fault_injection/` — 开发态故障注入场景（业务代码只调 `backend/app/fault_injection_gateway.py`）

---

## 4. 验证命令速查

```bash
# 后端单测（指定模块，不跑全量）
cd backend && python -m pytest tests/test_result_review_tool.py -x -q
cd backend && python -m pytest tests/agent_runtime/test_plan_repair_agent/ -x -q
cd backend && python -m pytest tests/agent_runtime/test_plan_incremental_agent/ -x -q
cd backend && python -m pytest tests/test_test_plan_regen_bulk.py -x -q

# 前端单测
cd frontend && npm run test

# 前端构建
cd frontend && npm run build

# 验证关键架构不变量
grep -r "_resolve_engine" backend/app/agent_runtime/api_dispatcher.py
grep -r "MemorySaver" backend/app/agent_runtime/persistence/
grep -r "publish_with_sink" backend/app/agent_runtime/event_publisher.py
grep -r "backfill_contract" backend/app/tools/result_review_tool.py
grep -r "_canonicalize_ai_targets" backend/app/tools/result_review_tool.py
grep -r "_config_alias_groups_from_request" backend/app/tools/test_plan_regen_tool.py

# 看关键模块文件树
ls backend/app/agent_runtime/
ls backend/app/context_engine/
ls backend/app/tools/

# 验证模块数（dev_3.0 实际值）
ls -d backend/app/context_engine/*/ | wc -l                # 应输出 22
ls backend/app/agent_runtime/graphs/test_plan/versions/v3/  # 应输出 11 个 nodes_*.py
ls backend/app/agent_runtime/dynamic_agent/                 # 业务实现
ls backend/app/agent_runtime/graphs/dynamic_agent/          # 图装配
```

→ 完整命令速查见 [CLAUDE.md § 3](CLAUDE.md)

---

## 5. 与其他文档的关系

| 文档 | 关系 |
|---|---|
| [README.md](README.md) | 项目入口，按角色/场景导航 |
| [CLAUDE.md](CLAUDE.md) | AI 协作指引（命令 / 护栏 / 布局速查） |
| [HANDO.md](HANDO.md) | 当前最新修复状态（活文档，随代码更新） |
| [docs_x/02](docs_x/02_TestAgent_项目总体技术方案.md) | 总体技术方案（**权威源**，本索引的章节跳转目标） |
| [docs_x/03](docs_x/03_TestAgent_项目实现原理与源码导读.md) | 源码导读（路径级，**操作指南**） |
| **docs_x/01**（本文）| **索引**：章节跳转 + 不变量证据 + 关键文件速查 |
| [docs/缺陷分析报告能力迁移/](docs/缺陷分析报告能力迁移/) | 缺陷分析能力迁移包（10 份专题文档） |
| [docs/开发帮助文档/](docs/开发帮助文档/) | Docker / 外部依赖等实操手册 |

> 📁 `docs/` 整体已 gitignore（除上述 2 个用户明确保留的子目录），**内部文档**不再纳入接手包。

---

## 6. 索引自检（dev_3.0）

- [x] 配套文档指向 docs_x/02/03 + README + CLAUDE.md + HANDO.md
- [x] 对应分支 `dev_3.0`
- [x] 章节跳转表覆盖 docs_x/02 全部 35 章 + docs_x/03 全部 23 章
- [x] 关键架构不变量 20 条（含 HANDO.md 关键修复：backfill_contract / 复审闭环 / section_1 映射 / 故障注入 / 真 sync interrupt / 不展示 CoT）
- [x] 关键文件速查按 12 个分类（入口 / 主图 / 子图 / CE / NC / DA / Tools / Checkpointer / SSE / 前端 / DB / Legacy）
- [x] 验证命令具体可执行（grep / pytest / ls）
- [x] PublicExecutionUpdateBuilder 标注为"保留为 fallback"，不是"已退役"（修正旧文档）
- [x] Context Engine 子模块数 = 22（修正 02/03 旧文档的 14+）
- [x] Dynamic Agent 描述区分业务实现（`agent_runtime/dynamic_agent/`）和图装配（`agent_runtime/graphs/dynamic_agent/`）
- [x] 与 HANDO.md（2026-08-24）状态对齐：ResultReviewTool backfill_contract / TestPlanRegenTool 复审 / section_1 映射修复 / 故障注入 / Narrative Composer / Conversation Timeline 根治都已收录
- [x] 不重复 02/03 的详细内容，只列证据位置
- [x] 每个证据指针给出 `file + 类/函数/字段` 形式（不强求行号）

**索引完成。配套见 [docs_x/02](docs_x/02_TestAgent_项目总体技术方案.md)（总览）+ [docs_x/03](docs_x/03_TestAgent_项目实现原理与源码导读.md)（源码导读）。**