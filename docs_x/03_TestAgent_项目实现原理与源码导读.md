# TestAgent 项目实现原理与源码导读

> **配套文档**
> - 总体技术方案:`docs/49_TestAgent_项目总体技术方案.md`
> - 证据索引:`docs/51_TestAgent_项目技术方案证据索引.md`

**编制时间**:2026-07-22（最后更新:2026-08-17 对齐 dev_3.0 真实状态）
**对应分支**:`dev_3.0`

> ⚠️ 本文档为新加入项目的开发人员提供**源码阅读路线**。每个文件都标注作用、核心函数、读取什么、输出什么、与谁调用。
> 自 2026-08-03 起,项目主线已从 `dev_2.0` 切换至 `dev_3.0`,**新增 4 大模块**:
> - **Context Engine 3.0**(`backend/app/context_engine/`)— **22 子模块**,CE-05 WP1-14 闭环
> - **Dynamic Agent**(`backend/app/agent_runtime/dynamic_agent/` + `app/agent/atomic_capability_registry.py`)— 11 节点 + 4 原子能力
> - **Narrative Composer**(`backend/app/agent_runtime/narrative_composer/`)— LLM-first 叙事 + 8 Tool 同步 Barrier
> - **Test Faults**(`backend/app/test_faults/`)— 5 种 dev 环境故障注入
>
> 另:v3 主图拓扑重构完成,节点从 13 个扩展到 30+ (新增 `tool_narrative_barrier` / `repair_subgraph` / `task_summary_narrative` / `format_loss_interrupt` 等)。

---

## 1. 阅读方法

阅读 TestAgent 源码,正确顺序是:

1. **入口优先**:`main.py` → `lifespan` → 看 FastAPI 怎么起、DB/Redis/Checkpointer 怎么初始化
2. **数据结构**:`models/` 和 `schemas/` → 看 MySQL 表怎么定义、Pydantic 怎么验
3. **调用链**:`api/` → `services/` → `repositories/` → 看一个请求从 HTTP 到 DB 的完整路径
4. **核心实现**:`agent_runtime/` → 看 LangGraph、EngineRouter、Worker 怎么跑
5. **Context Engine 3.0**(`app/context_engine/`):从 `runtime/context_engine.py` 入口看,逆推 Profile → Source → Composer → Preflight → LLM
6. **Dynamic Agent**(`agent_runtime/dynamic_agent/`):从 `graph.py` 入口看,跟随 Planner → Executor → Verifier → Replanner → Synthesizer
7. **Narrative Composer**(`agent_runtime/narrative_composer/`):从 `composer.py` 入口看,理解 LLM-first + Schema + Fact Validator + StreamDecoder
8. **工具与 Tool**:`tools/` → 看每个 Tool 的输入输出和失败处理(TestPlanGeneratorTool / TestPlanRegenTool / ResultReviewTool 已修复重要 Bug)
9. **事件与 SSE**:`event_publisher` → `useSse` → 看事件从前端推到后端再到前端的完整回路
10. **Timeline Assembly**:`useTaskEventReducer` → `conversationTimeline` → 看 task 事件如何通过统一 reducer 处理并组装到时间线

**不要**先读:
- `agent/orchestrator.py`(Legacy,~65KB,容易迷失)
- `agent_runtime/graphs/test_plan/versions/v3/nodes_*.py`(主图节点文件,直接看容易迷失)
- `frontend/src/components/cards/AgentRunCard.vue`(>2k 行,单文件太大)
- `agent_runtime/narrative_composer/composer.py`(~24kB,先看 HANDO §7.4 锚点)

先看上面的 1-3,把骨架搭起来,再回头看大文件就有上下文。

---

## 2. 项目目录总览(dev_3.0)

```
TestAgent/
├── backend/                                  # FastAPI 后端
│   ├── alembic/                              # DB 迁移
│   │   └── versions/                         # 各版本 .py 迁移
│   ├── app/
│   │   ├── main.py                           # FastAPI 入口 + lifespan
│   │   ├── api/v1/                           # REST 路由(auth/users/conversations/messages/files/...)
│   │   ├── core/                             # 配置、日志、异常
│   │   ├── db/                               # SQLAlchemy session、连接池
│   │   ├── models/                           # ORM 模型(每张表一个 .py)
│   │   ├── schemas/                          # Pydantic 入参/出参
│   │   ├── services/                         # 业务编排 + Agent Runtime Service
│   │   ├── repositories/                     # 数据访问层
│   │   ├── tools/                            # 9 个核心 Tool
│   │   ├── agent/                            # Legacy orchestrator + Atomic Capability
│   │   │   ├── orchestrator.py               # Legacy(~65KB,先跳过)
│   │   │   ├── atomic_capability_registry.py # Dynamic Agent 4 个原子能力
│   │   │   ├── retry_policy.py
│   │   │   ├── public_execution_update.py    # 旧确定性 builder 的数据结构
│   │   │   └── public_execution_update_builder.py  # 旧确定性 builder（保留为 fallback，主路径已由 NarrativeComposer 取代）
│   │   ├── agent_runtime/                    # LangGraph Runtime(第二阶段)
│   │   │   ├── api_dispatcher.py             # 引擎路由 + dispatch
│   │   │   ├── langgraph_run_coordinator.py
│   │   │   ├── graph_registry.py
│   │   │   ├── graphs/
│   │   │   │   ├── test_plan/
│   │   │   │   │   └── versions/v3/
│   │   │   │   │       ├── graph.py          # v3 主图装配(30+ 节点)
│   │   │   │   │       ├── entry_routing.py
│   │   │   │   │       ├── routing.py        # 边条件
│   │   │   │   │       ├── routing_after_interrupt.py
│   │   │   │   │       ├── nodes_pre_confirm.py
│   │   │   │   │       ├── nodes_post_confirm.py
│   │   │   │   │       ├── nodes_review_format.py
│   │   │   │   │       ├── nodes_narrative.py
│   │   │   │   │       ├── nodes_interrupts.py
│   │   │   │   │       ├── nodes_terminal.py
│   │   │   │   │       └── nodes_util.py
│   │   │   │   └── dynamic_agent/            # [dev_3.0] 动态 Agent 子图
│   │   │   │       ├── graph.py              # 11 节点主图
│   │   │   │       ├── state.py
│   │   │   │       └── constants.py
│   │   │   ├── dynamic_agent/                # [dev_3.0] Planner / Executor / Verifier / Replanner / Synthesizer
│   │   │   │   ├── planner.py
│   │   │   │   ├── executor.py
│   │   │   │   ├── verifier.py
│   │   │   │   ├── replanner.py
│   │   │   │   ├── synthesizer.py
│   │   │   │   ├── plan_validator.py
│   │   │   │   ├── completion_projector.py
│   │   │   │   ├── tool_handlers.py
│   │   │   │   ├── budgets.py
│   │   │   │   └── schemas.py
│   │   │   ├── narrative_composer/           # [dev_3.0] LLM-first 叙事层
│   │   │   │   ├── composer.py               # 生成 + 校验 + 1 次 repair + fallback
│   │   │   │   ├── context_builders.py       # per-Tool 事实压缩
│   │   │   │   ├── validator.py              # Schema + 事实约束
│   │   │   │   ├── stream_decoder.py         # Tagged Narrative Stream V1
│   │   │   │   ├── prompts.py
│   │   │   │   └── schemas.py
│   │   │   ├── _shared/                      # 跨模块共享 utils
│   │   │   │   ├── public_narrative.py       # normalize_public_update
│   │   │   │   ├── summary_facts.py          # Task Summary 事实构建
│   │   │   │   ├── to_fail_decision.py
│   │   │   │   ├── narrative_governance/     # 叙事治理 feature flags
│   │   │   │   ├── artifact_contract/
│   │   │   │   ├── args_signature.py
│   │   │   │   ├── budget.py
│   │   │   │   └── permission.py
│   │   │   ├── persistence/                  # Postgres Checkpointer + probe
│   │   │   ├── events/                       # AgentEvent + Live Event Bus
│   │   │   ├── canary/                       # 灰度 + rollback drill
│   │   │   ├── preparation/                  # Preparation 子图
│   │   │   ├── repair/                       # Repair 子图
│   │   │   ├── incremental/                  # Incremental 子图
│   │   │   ├── adapters/                     # ToolAdapter(白名单 + Identity Contract)
│   │   │   ├── context_runtime_builder.py
│   │   │   ├── context/                      # Context Runtime 工具
│   │   │   ├── sse/                          # SSE 路由
│   │   │   ├── observability/
│   │   │   └── graph_thread_id.py
│   │   ├── context_engine/                   # [dev_3.0 CE-05] CE 3.0
│   │   │   ├── __init__.py                   # ContextEngine Facade
│   │   │   ├── profiles/registry.py          # Profile Registry + Resolver
│   │   │   ├── sources/                      # 10+ Source Adapter
│   │   │   │   ├── conversation.py
│   │   │   │   ├── memory.py
│   │   │   │   ├── summary.py
│   │   │   │   ├── system_rules.py
│   │   │   │   ├── artifact.py
│   │   │   │   ├── file_document.py
│   │   │   │   ├── knowledge.py              # Maas KB → Knowledge Source
│   │   │   │   ├── task_state.py
│   │   │   │   ├── workspace_instruction.py
│   │   │   │   ├── orchestrator.py
│   │   │   │   ├── deadline.py
│   │   │   │   ├── production_registry.py
│   │   │   │   ├── registry.py
│   │   │   │   └── _base.py
│   │   │   ├── composer/                     # Composer + 校验
│   │   │   ├── runtime/                      # ContextEngine + EngineFactory + Bridge
│   │   │   ├── retrieval/                    # Retrieval + Audit + Executor
│   │   │   ├── scope/                        # Scope Resolver + Task Scope Validator
│   │   │   ├── models/                       # Context / Profile / Snapshot / Payload / ...
│   │   │   ├── indexing/                     # Lexical + Vector Store + Index Worker
│   │   │   ├── memory/                       # 长短期记忆
│   │   │   ├── snapshot/                     # Snapshot
│   │   │   ├── freeze/                       # Task Freeze
│   │   │   ├── planning/                     # Planning
│   │   │   ├── security/                     # CE 安全
│   │   │   ├── maintenance/                  # Retention Worker
│   │   │   ├── providers/                    # LLM Provider 适配
│   │   │   ├── shadow/                       # Shadow 评估
│   │   │   ├── selection/                    # Source Selection
│   │   │   ├── tool_output/                  # Tool Output Adapter
│   │   │   ├── payload/                      # Payload
│   │   │   ├── compression/                  # Preflight 压缩
│   │   │   ├── debug/                        # Debug API
│   │   │   ├── feature_flags.py
│   │   │   └── errors.py
│   │   ├── test_faults/                      # [dev_3.0] 5 种 dev 环境故障
│   │   │   └── __init__.py
│   │   ├── llm/                              # LLM client
│   │   ├── integrations/                     # 知识库 HTTP 客户端
│   │   ├── storage/                          # 文件系统存储抽象
│   │   └── utils/
│   ├── tests/
│   │   ├── agent_runtime/                    # Agent Runtime 测试
│   │   ├── tools/                            # Tool 测试
│   │   ├── context_engine/                   # [dev_3.0] CE 测试
│   │   ├── test_ce_*.py                      # CE 冒烟 / 矩阵 / 对话脚本
│   │   ├── test_fault_injection.py           # 5 种故障注入
│   │   ├── test_repair_parser_fix.py
│   │   ├── test_result_parser_lenient.py
│   │   ├── test_review_standard_rules.py
│   │   ├── test_test_plan_generator_retry.py
│   │   ├── test_tool_adapter_display_name.py
│   │   ├── test_dynamic_agent_*.py           # Dynamic Agent 测试套件
│   │   ├── test_chat_routing.py
│   │   ├── test_context_*.py
│   │   ├── test_message_repository_sequence_retry.py
│   │   └── ...
│   ├── pyproject.toml
│   └── requirements.txt
├── frontend/                                 # Vue 3 前端
│   ├── src/
│   │   ├── main.ts                           # 入口
│   │   ├── App.vue
│   │   ├── router/                           # Vue Router
│   │   ├── views/                            # 页面
│   │   ├── components/
│   │   │   ├── chat/                         # ChatWorkspace / ChatMessageList / ChatInputBox
│   │   │   ├── cards/                        # AgentRunCard / ToolCallMessage / PublicExecutionUpdate
│   │   │   ├── layout/                       # SidePanel / Header
│   │   │   ├── profile/
│   │   │   ├── knowledge/
│   │   │   ├── settings/
│   │   │   └── common/
│   │   ├── composables/                      # useSse / useTaskEvents / useTaskEventReducer / useFileUpload
│   │   ├── utils/                            # conversationTimeline / taskTiming / taskState / toolStatus
│   │   ├── stores/                           # Pinia stores(conversationStore / agentTaskStore / settingsStore / fileStore)
│   │   ├── api/                              # axios client + 各域 API
│   │   ├── types/                            # TypeScript 接口
│   │   ├── styles/                           # CSS variables
│   │   └── assets/
│   ├── package.json
│   └── tsconfig.json
├── docs/                                     # 项目文档
├── docker/                                   # Dockerfile + nginx 配置
├── docker-compose.dev.yml
├── docker-compose.prod.yml
├── docker-compose.test.yml
├── scripts/                                  # 运维脚本
├── data/                                     # dev 数据
├── alembic.ini
├── .env.example
└── HANDO.md                                  # 当前会话交接
```

---

## 3. 前端源码阅读路线

### 3.1 入口

| 顺序 | 文件 | 作用 | 核心 |
|---|---|---|---|
| 1 | `frontend/src/main.ts` | Vue 入口 | `createApp(App).use(router).use(pinia).mount('#app')` |
| 2 | `frontend/src/router/index.ts` | 路由 | 守卫 `beforeEach` 检查 token |
| 3 | `frontend/src/App.vue` | 顶层布局 | `<router-view>` |
| 4 | `frontend/src/views/ChatView.vue` | 主页面 | 调 ChatWorkspace |

### 3.2 Chat Workspace

```
ChatView.vue
└─ ChatWorkspace.vue              [components/chat/]
   ├─ SSE 连接 useSse(taskId)
   ├─ 事件处理 useTaskEvents(handler)
   │   └─ task 事件 → applyLiveTaskEvent() → reduceTaskEvent()
   ├─ ChatMessageList            渲染消息流
   │   └─ assembleConversationTimeline() → ConversationItem[]
   │      ├─ 普通消息 (rank=0)
   │      └─ AgentRunItem (rank=1, 从 TaskRunBlock 直接创建)
   │         └─ AgentRunCard     包含 plan/tool/retry timeline
   └─ ChatInputBox              输入 + 文件上传
```

| 顺序 | 文件 | 作用 | 核心函数 |
|---|---|---|---|
| 5 | `components/chat/ChatWorkspace.vue` | 主容器 | `setup()`, `handleSend()`, `connectSSE()` |
| 6 | `composables/useSse.ts` | SSE 客户端 | `connect()`, `parseSseBlock()`, `reconnect()` |
| 7 | `composables/useTaskEvents.ts` | Live 事件→消息 | `createTaskEventHandler()`, `eventDataToMessage()` |
| 7b | `composables/useTaskEventReducer.ts` | **[新]** 统一 TaskRunReducer | `reduceTaskEvent()`, `normalizeTaskEvent()` |
| 7c | `utils/conversationTimeline.ts` | **[新]** TimelineAssembler | `assembleConversationTimeline()` |

### 3.3 状态管理

| 顺序 | 文件 | 作用 |
|---|---|---|
| 8 | `stores/conversationStore.ts` | 会话 + 消息 + restore + task events (applyLiveTaskEvent) |
| 9 | `stores/agentTaskStore.ts` | 任务详情 + cancel/retry |
| 10 | `stores/authStore.ts` | 用户 token |

### 3.4 渲染

| 顺序 | 文件 | 作用 |
|---|---|---|
| 11 | `components/chat/ChatMessageList.vue` | 消息列表 + 分组 + scroll |
| 12 | `components/cards/AgentRunCard.vue` | agent_run 容器(2393 行) |
| 13 | `components/cards/ToolCallMessage.vue` | tool_call 卡片(92 行) |
| 14 | `components/cards/PublicExecutionUpdate.vue` | publicUpdate 渲染(137 行,**[开发中]**) |
| 15 | `components/cards/RequirementSummaryCard.vue` | 需求摘要 |
| 16 | `components/cards/SectionConfirmCard.vue` | 章节确认(Interrupt) |
| 17 | `components/cards/ArtifactDownloadCard.vue` | 产物下载 |

### 3.5 API

| 顺序 | 文件 | 作用 |
|---|---|---|
| 18 | `api/client.ts` | axios + Bearer interceptor |
| 19 | `api/auth.ts` `api/chat.ts` `api/files.ts` `api/agent.ts` 等 | 各域 API |

---

## 4. 后端启动源码路线

### 4.1 `main.py` → `lifespan`

`backend/app/main.py`:

```python
load_dotenv(override=True)              # 关键:dev 用 override=True
get_settings.cache_clear()              # 清 pydantic LRU

app = FastAPI(lifespan=lifespan)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 初始化 DB engine
    # 2. 初始化 Redis client (可选)
    # 3. 探活 Postgres Checkpointer (probe)
    # 4. 编译 LangGraph graph registry
    # 5. 初始化 dispatcher / EngineRouter
    # 6. 启动 AgentExecutionWorker 后台 poll
    yield
    # 关闭:停 worker,关 DB
```

### 4.2 配置

| 顺序 | 文件 | 作用 |
|---|---|---|
| 1 | `backend/app/core/config.py` | Pydantic Settings,所有 env 变量 |
| 2 | `backend/.env` | dev 配置(已 gitignore) |

### 4.3 DB 与 Redis

| 顺序 | 文件 | 作用 |
|---|---|---|
| 3 | `backend/app/db/session.py` | SQLAlchemy engine + session factory |
| 4 | `backend/app/db/redis_client.py` | Redis async client |

### 4.4 Checkpointer

| 顺序 | 文件 | 作用 |
|---|---|---|
| 5 | `backend/app/agent_runtime/persistence/postgres_checkpointer.py` | `normalize_postgres_conn_string()`, `build_postgres_checkpointer()` |
| 6 | `backend/app/agent_runtime/persistence/probe_router.py` | probe + 双闸门 lockout |

### 4.5 Graph Registry

| 顺序 | 文件 | 作用 |
|---|---|---|
| 7 | `backend/app/agent_runtime/graph_registry.py` | `compile_test_plan_graph("v3")` / `"v2_frozen"` |
| 8 | `backend/app/agent_runtime/graphs/test_plan/versions/v3/graph.py` | 实际图节点组装 |

### 4.6 Dispatcher & Worker

| 顺序 | 文件 | 作用 |
|---|---|---|
| 9 | `backend/app/agent_runtime/api_dispatcher.py` | `dispatch_new_task()`, `dispatch_from_outbox_row()`, `EngineRouter` |
| 10 | `backend/app/services/agent_execution_worker.py` | `_poll_once()`, `claim_next()` |

---

## 5. API 到 Service 路线(5 个关键接口)

### 5.1 登录

```
POST /api/v1/auth/login
↓
api/v1/auth.py:login()
↓
services/auth_service.py:AuthService.login(email, password)
↓
repositories/user_repository.py:UserRepository.find_by_email()
↓
models/user.py:User.verify_password(password)
↓
return access_token (JWT)
```

### 5.2 文件上传

```
POST /api/v1/files (multipart)
↓
api/v1/files.py:upload()
↓
services/file_service.py:FileService.upload(user, file)
↓
services/atomic_file_writer.py:AtomicFileWriter.write(tmp, content)  # tmp + fsync + rename
↓
repositories/file_repository.py:FileRepository.create(user, file_meta)
↓
return file_id
```

### 5.3 用户发消息(创建任务)

```
POST /api/v1/messages/stream
↓
api/v1/messages.py:send_message()
↓
services/message_service.py:MessageService.append_message(...)
↓
services/agent_task_service.py:AgentTaskService.create_task(user, conv, message)
↓
agent_runtime/api_dispatcher.py:EngineRouter.decide()        # 决定 engine
↓
repositories/agent_task_repository.py:create(engine_type)
↓
repositories/agent_execution_request_repository.py:create_outbox(task_id)
↓
return task_public_id
```

### 5.4 SSE

```
GET /api/v1/agent-tasks/{id}/events  (Bearer token)
↓
api/v1/agent_tasks.py:stream_events()
↓
services/auth_service.verify_token
↓
repositories/agent_task_repository.find_by_public_id_and_user()
↓
agent_runtime/api_drainer.HistoryDrainer(task_id).drain()       # 读 MySQL 历史
↓
agent_runtime/event_publisher.subscribe(task_id)               # 订阅 Redis Live Bus
↓
SSE: yield f"event: {type}\ndata: {json}\nid: {public_id}\n\n"
```

### 5.5 用户确认

```
POST /api/v1/confirmations/{id}/approve
↓
api/v1/confirmations.py:approve()
↓
services/confirmation_service.approve(...)
↓
repositories/confirmation_repository.update(approved)
↓
agent_runtime/langgraph_run_coordinator.resume(ctx, decision)
↓
PostgreSQL checkpoint.get(thread_id) → 继续图
```

---

## 6. Agent 任务创建源码路线

| 文件 | 类/函数 | 输入 | 输出 | 下一步 |
|---|---|---|---|---|
| `api/v1/messages.py` | `send_message()` | `{conversation_id, content, file_ids}` | `task_public_id` | 客户端订阅 SSE |
| `services/message_service.py` | `MessageService.append_message()` | user, conversation, content, files | message, task | 创建 AgentTask |
| `services/agent_task_service.py` | `AgentTaskService.create_task()` | user, conversation, message | AgentTask | 创建 Outbox |
| `agent_runtime/api_dispatcher.py` | `EngineRouter.decide()` | task spec | engine_type | 选引擎 |
| `repositories/agent_execution_request_repository.py` | `create_outbox()` | task_id | request row | Worker poll |
| `services/agent_execution_worker.py` | `_poll_once()` | – | dispatch 调用 | 进入引擎 |

---

## 7. LangGraph 运行源码路线

```
services/agent_execution_worker._poll_once()
  ↓
agent_runtime/api_dispatcher.dispatch_from_outbox_row(row)
  ↓ EngineRouter
agent_runtime/langgraph_dispatch_adapter.dispatch(ctx)
  ↓
agent_runtime/langgraph_run_coordinator.run_pre_confirm(ctx)
  ↓ 创建 AgentContext
agent_runtime/langgraph_run_coordinator.ainvoke(ctx)
  ↓
agent_runtime/graph_registry.compile_test_plan_graph("v3")
  ↓
compiled_graph.ainvoke(state, config={"configurable": ctx, "thread_id": task.public_id})
  ↓
node functions 执行
  ↓ 写 AgentEvent
  ↓ 写 Checkpoint (PG)
END
```

---

## 8. Graph v3 源码路线(dev_3.0 真实结构)

```
agent_runtime/graphs/test_plan/
├── __init__.py
├── state.py                          # TestPlanGraphState TypedDict
├── graph.py                          # 主图(v1/v2)
├── versions/
│   ├── v2_frozen/                    # 历史版本,只读
│   └── v3/
│       ├── __init__.py
│       ├── graph.py                  # compile_test_plan_graph_v3()(30+ 节点)
│       ├── state.py
│       ├── entry_routing.py          # 入口路由(entry_router)
│       ├── routing.py                # 边条件(route_after_*)— validate/narrative/parse_template/review/format_check/export_word/generate_test_plan/result_review
│       ├── routing_after_interrupt.py  # format_interrupt 路由
│       ├── nodes_interrupts.py       # 真 interrupt(section_confirmation_interrupt / format_loss_interrupt)与 sentinel pause
│       ├── nodes_pre_confirm.py      # 准备阶段节点(initialize_task / validate_inputs / parse_requirement / parse_template / prep_subgraph / prep_legacy_fallback / search_knowledge / suggest_sections / prepare_section_confirmation / pause_for_legacy_confirm / resume_task)
│       ├── nodes_post_confirm.py     # 生成后节点(generate_test_plan / prepare_export / export_word / pause_for_legacy_format_decision / record_loss_decision / finalize_task)
│       ├── nodes_review_format.py    # 审查与修复节点(review_step / regenerate_sections / repair_subgraph / repair_fallback / check_docx_format)
│       ├── nodes_narrative.py        # [Phase 2.9B.4] 叙事同步屏障(tool_narrative_barrier / task_summary_narrative)
│       ├── nodes_terminal.py         # 终态(fail_task / cancel_task)
│       └── nodes_util.py             # 共享工具(get_ctx)
```

每个 Node 都是 Python 函数,签名 `async def node(state: TestPlanGraphState, ctx) -> dict`。
返回值会被合并回 State,sync interrupt 节点用 `_bind_sync` 包装。

### 8.1 v3 节点装配链路(实际数据流)

```
START → entry_router
  ├─ 新任务 → initialize_task → validate_inputs → parse_requirement
  │                                          ├─ valid → tool_narrative_barrier
  │                                          └─ invalid → fail_task
  │                  tool_narrative_barrier (route_after_narrative 多分支)
  │  ├─ 后续 Tool 节点(parse_template / search_knowledge / suggest_sections)
  │  ├─ → parse_template → barrier → PrepSubgraph
  │  │
  │  PrepSubgraph → prep_legacy_fallback → suggest_sections → barrier
  │  │
  │  barrier → prepare_section_confirmation → section_confirmation_interrupt
  │       (真 interrupt,resume 后直连 generate_test_plan)
  │  │
  │  generate_test_plan → barrier → review_step → barrier
  │  ├─ issues → repair_subgraph → prepare_export
  │  ├─ issues → regenerate_sections → review_step
  │  └─ pass → prepare_export
  │  │
  │  prepare_export → export_word → barrier → check_docx_format
  │       ├─ loss → format_loss_interrupt → task_summary_narrative
  │       └─ clean → task_summary_narrative
  │  │
  │  task_summary_narrative → finalize_task → END
  │  │
  │  fail_task → END
  │  cancel_task → END
  │
  ├─ resume → resume_task → generate_test_plan (从 Interrupt 恢复)
  └─ legacy decision → record_loss_decision → task_summary_narrative
```

### 8.2 Dynamic Agent 子图(`agent_runtime/graphs/dynamic_agent/graph.py`)

```
START → initialize → create_plan → validate_plan
  ├─ awaiting_user → need_user → END
  ├─ failure → fail → END
  └─ valid → execute_next_step
       ├─ 还有 pending step → execute_next_step
       └─ 无 pending step → verify_goal
                      ├─ COMPLETE → synthesize → persist_final_answer → finalize → END
                      ├─ CONTINUE → execute_next_step
                      ├─ REPLAN → replan(budget ≤2) → execute_next_step
                      ├─ NEED_USER → need_user → END
                      └─ 其他 → fail → END
```

**关键文件**:
- `graph.py` ~930 行,11 节点 + build_dynamic_agent_v1_graph()
- `agent_runtime/dynamic_agent/planner.py` DynamicPlanner(LLM + deterministic fallback)
- `agent_runtime/dynamic_agent/executor.py` DynamicStepExecutor(bridge / deterministic 双模式)
- `agent_runtime/dynamic_agent/verifier.py` DynamicVerifier 四态判定
- `agent_runtime/dynamic_agent/replanner.py` DynamicReplanner(Gap-driven)
- `agent_runtime/dynamic_agent/synthesizer.py` DynamicSynthesizer
- `app/agent/atomic_capability_registry.py` AtomicCapabilityRegistry.default() 4 个原子能力

---

## 9. Preparation 源码路线(以 KnowledgeSearch 调用为例)

```
graph.ainvoke(state, config)
  ↓ preparation_subgraph entry
preparation/entry.py:prepare_context(state)
  state.kb_needed = ["性能", "安全"]  # LLM 决定缺什么
  return {"kb_needed": [...]}
  ↓
preparation/llm_decide.py:decide(state, config)
  messages = [{role: system, content: prep_decide_prompt}, {role: user, content: json.dumps(state.kb_needed)}]
  response = ChatLLMService.chat(messages)
  action = json.loads(response)  # {"action": "tool_call", "tool": "KnowledgeSearchTool", "input": {"query": "..."}}
  return {"next_action": action}
  ↓
preparation/tool_node.py:run(action, state, config)
  result = ToolExecutor.run(action.tool, action.input, ctx)
  return {"kb_results": state.kb_results + [result]}
  ↓
preparation/observe.py:observe(state)
  if state.kb_results 覆盖 kb_needed:
      return {"next_action": {"action": "finish"}}
  else:
      back to llm_decide
```

---

## 10. Repair 源码路线(以一个 ReviewIssue 修复为例)

```
review_node.run(state) → ResultReviewTool → review_issues = [{section: "性能测试", type: "missing_coverage", severity: high}]
  ↓
repair_subgraph entry
repair/scope_guard.py:filter_repairable(issues, locked_sections)
  repairable = issues that section not in locked_sections
  return {"repairable_issues": [...]}
  ↓
repair/llm_plan.py:repair_plan(issues)
  LLM 决定改哪个 section → regen_targets = ["性能测试"]
  return {"regen_targets": [...]}
  ↓
repair/regen_node.py:regen(state)
  for section in regen_targets:
      new_content = TestPlanRegenTool.run({section, feedback: issue})
  return {"generated_sections[section]": new_content}
  ↓
repair/re_review.py:re_review(state)
  new_issues = ResultReviewTool.run(state)
  if new_issues: back to scope_guard(loop)
  else: finish
```

---

## 11. Incremental 源码路线(以"只修改性能测试章节"为例)

```
incremental entry (source_artifact_id)
  ↓
incremental/read_old.py:load_artifact(source_artifact_id)
  return {"old_sections": {...}}
  ↓
incremental/diff.py:diff(old, user_request)
  LLM 决定 changed_sections = ["性能测试"], locked = others
  return {"changed": [...], "locked": [...]}
  ↓
incremental/regen.py:incremental_regen(state)
  for section in changed:
      new_content = TestPlanRegenTool.run({section, old: state.locked[section]})
  return {"new_sections": {section: new_content}}
  ↓
incremental/merge.py:merge(old + new)
  final = old.update(new)  # locked sections keep as-is
  return {"final_sections": final}
  ↓
finish
```

---

## 12. Tool 源码路线

### 12.1 BaseTool

`backend/app/tools/base.py`:

```python
class BaseTool(ABC):
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]

    @abstractmethod
    async def run(self, inputs: dict, ctx: AgentContext, retry_context: RetryContext | None) -> dict:
        ...

    def to_openai_tool(self) -> dict:
        # 转 OpenAI tool 格式
        ...
```

### 12.2 ToolExecutor

`backend/app/tools/executor.py`:

```python
class ToolExecutor:
    def __init__(self, tools: list[BaseTool]):
        self._tools = {t.name: t for t in tools}

    async def run(self, name: str, inputs: dict, ctx: AgentContext, retry_context=None) -> dict:
        tool = self._tools[name]
        validated = tool.input_schema.model_validate(inputs)
        result = await tool.run(validated.model_dump(), ctx, retry_context)
        return tool.output_schema.model_validate(result).model_dump()
```

### 12.3 ToolAdapter

`backend/app/agent_runtime/adapters/test_agent_tool_adapter.py`:

```python
class TestAgentToolAdapter:
    def __init__(self, whitelist: list[str]):
        self._whitelist = set(whitelist)

    def wrap(self, tool_call: dict) -> ToolCall:
        if tool_call["name"] not in self._whitelist:
            raise ToolNotAllowed(tool_call["name"])
        return ToolCall(name=tool_call["name"], args=tool_call["args"], envelope_version=1)
```

---

## 13. WordExport 源码路线

```
agent_runtime/graphs/.../nodes/word_export.py:word_export(state, config)
  ↓
tools/word_export_tool.py:WordExportTool.run(inputs, ctx, retry_context)
  ├─ 校验 inputs.section_package (Pydantic)
  │   → 失败 EXPORT_CONTENT_MISSING (UNRECOVERABLE)
  ├─ 读模板 → 失败 EXPORT_TEMPLATE_NOT_FOUND (UNRECOVERABLE)
  ├─ 准备 tmp_path = output_path + ".tmp." + uuid.uuid4().hex[:8]
  ├─ python-docx 写入 tmp
  │   → 失败 EXPORT_FAILED (recoverable=True)
  ├─ os.fsync(fp.fileno())
  ├─ os.replace(tmp, output)  # 原子
  ├─ ArtifactService.create_or_get_by_idempotency_key(...)
  │   → 已存在 → 返回旧 artifact_id
  │   → 不存在 → 写 MySQL artifacts + 落 file
  └─ return {"success": True, "artifact_id": ...}
```

Retry 链路见 `docs/49` §20.2。

---

## 14. Checkpoint 源码路线

```
agent_runtime/langgraph_run_coordinator.ainvoke(ctx)
  ├─ compiled = graph_registry.compile_test_plan_graph(graph_version)
  ├─ config = {
  │     "configurable": {"thread_id": ctx.task.public_id, ...},
  │     "recursion_limit": BusinessBudgets.recursion_limit,
  │   }
  └─ compiled.ainvoke(initial_state, config)
      ↓ LangGraph 内部
      PostgresSaver.get_tuple(config)  # 加载 history
      PostgresSaver.put_writes(...)   # 每个 Node 后
      AsyncPostgresSaver.setup()      # 首次建表
```

Probe:`persistence/probe_router.py` 启动时调 `checkpointer.setup()`,失败 → 双闸门 lockout。

---

## 15. Event 源码路线

```
node 函数内部
  └─ await ctx.event_publisher.publish(
        ctx,
        event_type=AgentEventType.TOOL_FINISHED,
        payload=chunk_frame,
     )
↓
agent_runtime/event_publisher.py:EventPublisher.publish()
  ├─ EventRepository.append(task_id, event_type, payload)
  │   → 分配 sequence_no (Redis INCR 兜底)
  │   → 写 MySQL agent_events
  ├─ LiveAgentEventSink.publish(task_id, event)
  │   ├─ InMemory: 内存 fan-out
  │   └─ Redis: PUBLISH live_bus:{task_id}
  └─ return event
```

前端接收:
```
useSse.connect(url)
  ↓ EventSource.onmessage
  parseSseBlock(text)  # 解析 event: + data: + id:
  createTaskEventHandler.handleEvent(parsed)
  └─ eventTypeToMessageType(event_type)
  └─ eventDataToMessage(...)  # 附加 _rawEvent
  └─ if (message.taskId)
  │    conversationStore.applyLiveTaskEvent()  # → reduceTaskEvent()
  │  else
  │    conversationStore.appendMessages([msg])
  ↓ Vue reactive update
```

---

## 16. SSE 源码路线

### 16.1 后端 Frame

```
api/v1/agent_tasks.py:stream_events(task_public_id, current_user)
  ├─ HistoryDrainer(task_id).drain()
  │   for event in agent_events (ordered by sequence_no):
  │       yield format_sse(event)
  ├─ event_bus.subscribe(task_id) → async iterator
  └─ for live_event in sub:
        yield format_sse(live_event)

def format_sse(event):
    return f"event: {event.type}\nid: {event.public_id}\ndata: {json.dumps(event.payload)}\n\n"
```

### 16.2 前端 Parser

```
composables/useSse.ts:parseSseBlock(chunk)
  for line in chunk.split("\n"):
      if line.startsWith("event:"): current_event = line[6:].strip()
      if line.startsWith("data:"): current_data += line[5:].strip()
      if line.startsWith("id:"): current_id = line[3:].strip()
      if blank line: emit (event, data, id) tuple
```

⚠️ 当前 `parseSseBlock` 不解析 `id:` 也不解析 `retry:`。

---

## 17. Narrative Composer 源码路线(Phase 2.9B.4-7,LLM-first)

### 17.1 后端 LLM 叙事链路

```
v3 主图 Tool 节点 __.run(...) returns ToolResult
  ↓
ToolAdapter.wrap(...) — 回写真实 tool_call_id + last_terminal_event_id
  ↓
narrative_composer.composer.build_for_tool_result(tool, success, data)
  ├─ NarrativeComposer._generate_with_stream(tool_ctx, task_ctx)
  │   ├─ prompt = composer.prompts.build_tool_prompt(...)
  │   ├─ raw = await llm_client.generate_with_stream(...)
  │   └─ async for chunk in stream:
  │         compose_streaming_update(chunk) → emit partial PublicUpdate
  ├─ NarrativeValidator(schema + 事实约束)
  │   ├─ 数字白名单校验
  │   ├─ 文件名校验
  │   ├─ 状态语义校验
  │   └─ 敏感信息正则替换
  ├─ 1 次 schema_feedback repair
  ├─ 失败 → _fallback_result_from(tool, success, data)
  │   └─ source = "deterministic"
  └─ return PublicExecutionUpdate(narrative_source="llm"/"deterministic", ...)

v3 主图 tool_narrative_barrier 节点:
  ├─ await pending_narrative.finish()
  ├─ 持久化 AgentEvent(tool_narrative_update)
  └─ 路由下一节点
```

### 17.2 Task Summary 真实性校验

```
v3 主图 task_summary_narrative node:
  ├─ context = _shared.summary_facts.build_summary_facts(state)
  │   ├─ block_count / block_issues / review_issues / suggestions
  │   ├─ tool_failures != task_failure 区分
  │   ├─ artifact_ids / artifact_status
  │   └─ 任务级关键事实白名单
  ├─ NarrativeComposer._generate_task_summary(context)
  │   ├─ schema + fact validator 校验
  │   ├─ "blocking>0 却说无阻塞问题" 等事实冲突拒绝
  │   └─ 失败 → deterministic fallback
  └─ 写 AgentEvent(task_summary_narrative, narrative_source=...)
```

### 17.3 前端消费

```
useTaskEvents.eventDataToMessage(...)
  ├─ payload = data.payload ?? data.payload_json ?? data
  ├─ if event_type in [tool_narrative_update, task_summary_narrative]:
  │   ├─ publicUpdate = PublicExecutionUpdate.from_dict(payload.public_update)
  │   ├─ narrativeSource = payload.narrative_source  # llm / deterministic
  │   ├─ chunkIndex = payload.chunk_index
  │   ├─ if chunkIndex < stored: drop (out-of-order)
  │   └─ store: { toolCall.publicUpdate, narrativeSource }
  └─ conversationStore.applyLiveTaskEvent(convId, taskId, rawEvent)
       └─ reduceTaskEvent(block, event, 'live') → 更新 TaskRunBlock

useTaskEventReducer:reduceTaskSummaryNarrative(block, event)
  ├─ block.taskSummaryNarrative = parsed
  ├─ 唯一槽位,task_completed 不得清空
  └─ AgentRunCard.completionSummary = validLlmTaskSummary ?? deterministicTaskSummary
```

### 17.4 Tool Identity Contract(避免「执行了但未显示」)

```
backend/app/agent_runtime/adapters/test_agent_tool_adapter.py:
  result["tool_call_id"] = tool_call_id        ← 回写真实 ID
  result["last_terminal_event_id"] = ...       ← 真实终态 event_id

backend/app/agent_runtime/graphs/test_plan/versions/v3/nodes_*.py:
  source_event_id = adapter.last_terminal_event_id()  ← 锚点
  _build_pending_narrative(source_event_id=...)

前端匹配点:
  AgentRunCard.displayToolUpdate: 互斥选择器(LLM / fallback 互斥)
  toolNarrativeFor(toolCallId, attempt) 来源于 narrativeAnchor
```

### 17.5 配置开关

```bash
# .env(dev 默认 0,需显式打开)
AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_ENABLED=1
```

关闭时所有 Tool 走 `public_execution_update_builder`(老确定性路径)作为 fallback,但 `task_summary_narrative` 仍由确定性 formatter 输出。

### 17.6 测试覆盖

- `backend/tests/agent_runtime/test_phase29b4_narrative_composer.py` — composer 合同
- `backend/tests/agent_runtime/test_phase29b5_narrative_contract_fix.py` — 合同校验
- `backend/tests/agent_runtime/test_phase29b6_tool_identity_and_summary_facts.py` — 工具身份 + 事实
- `backend/tests/test_tool_adapter_display_name.py` — Tool 名称中文映射
- `frontend/src/composables/phase29b4_tool_narrative.spec.ts`
- `frontend/src/composables/phase29b5_state_isolation.spec.ts`
- `frontend/src/composables/phase29b6_tool_identity_and_summary_facts.spec.ts`
- `frontend/src/utils/taskState.spec.ts`

---

## 18. 数据库源码路线

```
backend/app/models/<entity>.py        # SQLAlchemy ORM
backend/app/repositories/<entity>_repository.py   # CRUD + 业务查询
backend/app/services/<entity>_service.py          # 跨 repo 编排
backend/app/schemas/<entity>.py                   # API 入出参
backend/alembic/versions/<rev>_<name>.py          # 迁移
```

每个新表的标准流程:
1. `models/<entity>.py` 定义 ORM
2. `repositories/<entity>_repository.py` 加 CRUD
3. `services/<entity>_service.py` 加业务方法
4. `alembic revision --autogenerate -m "add <entity>"`
5. `alembic upgrade head`
6. 测试

---

## 19. Retry 源码路线

```
orchestrator.py:Tool.run() 抛 RecoverableError
  ↓
_run_tool_with_retry.catch
  decision = retry_policy.decide(error, attempt, ctx)
  ├─ if decision.action == "retry":
  │     backoff = decision.backoff_seconds
  │     asyncio.sleep(backoff)
  │     _publish(RETRYING event with public_execution_update)
  │     return _run_tool_once(attempt+1)
  └─ if decision.action == "stop":
        _publish(TOOL_FAILED)
        _fail_task()
```

`RetryPolicy.decide()`:
```python
def decide(self, error: ToolError, attempt: int, ctx: RetryContext) -> RetryDecision:
    code = error.code
    if code in UNRECOVERABLE_ERROR_CODES:
        return RetryDecision(action="stop", strategy="hard_stop")
    if code in SCHEMA_ERROR_CODES:
        return RetryDecision(action="retry", strategy="schema_feedback", backoff_seconds=0)
    if code in NETWORK_ERROR_CODES:
        return RetryDecision(action="retry", strategy="backoff", backoff_seconds=2 ** attempt)
    if code in DEGRADABLE_ERROR_CODES:
        return RetryDecision(action="retry", strategy="degrade", backoff_seconds=0)
    if error.recoverable:
        return RetryDecision(action="retry", strategy="same_inputs", backoff_seconds=0.5)
    return RetryDecision(action="stop", strategy="hard_stop")
```

---

## 20. 取消和恢复源码路线

### 20.1 取消

```
api/v1/agent_tasks.py:cancel(task_public_id)
  ↓
services/agent_task_service.cancel()
  ├─ cancellation_distributed.signal(task_id) → Redis SET cancel:{task_id}
  ├─ agent_tasks.update(status=cancelling)
  └─ (异步) Worker poll 时检查 → coordinator.cancel(ctx) → PG checkpoint 标 cancel
```

### 20.2 Resume

```
api/v1/confirmations.py:approve(confirmation_id)
  ├─ confirmations.update(approved)
  └─ langgraph_run_coordinator.resume(ctx, decision)
      ├─ PG checkpoint.get(thread_id)
      ├─ 找到 Interrupt 点
      └─ compiled.ainvoke(state, config, resume=decision)
```

### 20.3 Worker Recovery

```
worker._poll_once()
  ├─ claim_next() → SELECT FOR UPDATE SKIP LOCKED
  ├─ row.claimed_until = now() + lease_seconds
  ├─ dispatch_from_outbox_row(row)
  │   └─ coordinator.ainvoke(ctx)
  │       └─ PG checkpoint.put_writes(...)
  └─ on success: mark completed
```

### 20.4 Lease Expiration

```
如果 worker 崩溃,row.claimed_until 过期 → 下次 poll 自动可被其他 worker 领取
新 worker:
  ├─ claim_next() 拿到该 row
  ├─ coordinator.ainvoke(ctx)
  │   └─ PG checkpoint.get → 恢复 state
  └─ 继续执行
```

---

## 21. 测试源码路线(dev_3.0)

| 想验证什么 | 看哪个测试 |
|---|---|
| 节点逻辑 | `tests/agent_runtime/graphs/test_plan/versions/v3/test_nodes_<name>.py` |
| v3 路由 | `tests/agent_runtime/graphs/test_plan/test_routing.py` |
| v3 Interrupt(2.9A.7 真 interrupt) | `tests/agent_runtime/test_interrupt_resume.py` |
| Checkpointer 恢复 | `tests/agent_runtime/test_persistence_postgres_probe.py` |
| Worker 领取 | `tests/agent_runtime/test_dispatch/test_dispatch_from_outbox.py` |
| Worker 轮询 | `tests/agent_runtime/test_dispatch/test_agent_execution_worker.py` |
| Engine 路由 | `tests/agent_runtime/test_canary/test_engine_router_decisions.py` |
| 双 Worker E2E | `tests/agent_runtime/test_canary/test_rollback_drill_e2e.py` |
| SSE 解耦 | `tests/agent_runtime/test_dispatch/test_sse_decoupled_from_execution.py` |
| Artifact 幂等 | `tests/agent_runtime/test_artifact_idempotency.py` |
| Tool 单测 | `tests/tools/test_<tool_name>.py` |
| **TestPlanGenerator 重试 / 表头** | `tests/test_test_plan_generator_retry.py` |
| **TestPlanRegenTool self-healing** | `tests/agent_runtime/test_plan_repair_agent/test_repair_parser_fix.py` |
| **ResultReviewTool 语义/工具失败** | `tests/test_review_standard_rules.py` |
| **ResultParser 解析宽松** | `tests/test_result_parser_lenient.py` |
| **Test Faults 故障注入** | `tests/test_fault_injection.py` |
| **Tool 名称中文映射** | `tests/test_tool_adapter_display_name.py` |
| **Message Repository 重试** | `tests/test_message_repository_sequence_retry.py` |
| **Narrative Composer 合同** | `tests/agent_runtime/test_phase29b4_narrative_composer.py` |
| **Narrative Contract 校验** | `tests/agent_runtime/test_phase29b5_narrative_contract_fix.py` |
| **Tool Identity + Summary Facts** | `tests/agent_runtime/test_phase29b6_tool_identity_and_summary_facts.py` |
| **Context Engine 3.0** | `tests/context_engine/` + `tests/test_ce_*.py` |
| **CE Composer 渲染** | `tests/context_engine/test_context_engine_composer.py` |
| **CE Profile Resolver** | `tests/context_engine/test_context_engine_control_plane.py` |
| **CE Invoker Snapshot 生命周期** | `tests/context_engine/test_context_engine_invoker.py` |
| **CE Runtime** | `tests/context_engine/test_context_engine_runtime.py` |
| **CE Settings** | `tests/context_engine/test_context_engine_settings.py` |
| **CE Preflight 压缩水位** | `tests/context_engine/test_ce04_preflight.py` |
| **CE Flag 行为** | `tests/test_ce03_flags.py` |
| **CE TestPlan Generation Path** | `tests/test_ce_test_plan_generation_path.py` |
| **Maas KB 降级链路** | `tests/test_maas_kb_degraded_flow.py` |
| **ContextInvokerBridge** | `tests/test_ce04_invoker_bridge.py` |
| **API 契约一致性** | `tests/test_api_contract_reconciliation.py` |
| **Request Understanding 路由** | `tests/test_request_understanding_routing.py` |
| **Chat Routing** | `tests/test_chat_routing.py` |
| **Context Learning 修复** | `tests/test_context_learning_fix.py` |
| **Context Usage** | `tests/test_context_usage.py` |
| **Conversation Summary** | `tests/context_engine/test_conversation_summary_service.py` |
| **Tool Lifecycle 2.9A.19** | `tests/test_phase29a19_tool_lifecycle.py` |
| **Dynamic Agent 协调器** | `tests/test_dynamic_agent_coordinator.py` |
| **Dynamic Agent 骨架** | `tests/test_dynamic_agent_foundation.py` |
| **Dynamic Agent 图运行** | `tests/test_dynamic_agent_graph_runtime.py` |
| **Dynamic Agent Runtime Core** | `tests/test_dynamic_agent_runtime_core.py` |
| **Dynamic Agent 完成投影** | `tests/test_dynamic_agent_completion_projector.py` |
| **Dynamic Agent Synthesizer 隐私** | `tests/test_dynamic_agent_synthesizer_privacy.py` |
| **Dynamic Agent 工具集成** | `tests/test_dynamic_agent_tool_integration.py` |
| 前端 Vitest | `frontend/src/**/*.spec.ts` |

---

## 22. 常见修改任务导航

### 22.1 我要增加一个 Tool

**改**:
1. 新增 `backend/app/tools/my_tool.py` 继承 `BaseTool`
2. `backend/app/agent_runtime/adapters/test_agent_tool_adapter.py` 加白名单
3. `backend/app/tools/executor.py` 注入新 tool
4. `backend/app/tools/__init__.py` 导出
5. `backend/tests/tools/test_my_tool.py` 写测试
6. 如果需要 LLM:`backend/app/llm/prompts/my_tool_prompt.py` 加 prompt
7. 如果涉及 Narrative:`backend/app/agent_runtime/narrative_composer/context_builders.py` 加 per-Tool Context Builder

**不要改**:
- `agent_runtime/api_dispatcher.py`(协议层)
- `agent_runtime/graph_registry.py`(图注册)
- LangGraph 主图

### 22.2 我要增加一个 Node

**改**:
1. `agent_runtime/graphs/test_plan/versions/v3/nodes_<file>.py`(pre_confirm / post_confirm / review_format / narrative)
2. `versions/v3/graph.py` 注册 + 加边
3. `versions/v3/state.py` 加 state 字段(若需要)
4. `tests/agent_runtime/graphs/test_plan/versions/v3/test_<file>.py`

**不要**改 v2_frozen。

### 22.3 我要增加一个事件类型

**改**:
1. `agent_runtime/events/event_types.py` 加 `AgentEventType.MY_EVENT`
2. 前端 `frontend/src/types/index.ts` 加
3. `useTaskEvents.ts:eventTypeToMessageType` 加映射
4. 后端某处 `_publish(AgentEventType.MY_EVENT, ...)`

### 22.4 我要增加一个 Artifact 类型

**改**:
1. `models/artifact.py` 加 `kind` enum
2. Alembic 迁移
3. `services/artifact_service.py` 加 create_or_get
4. `api/v1/artifacts.py` 加 download 端点

### 22.5 我要新增前端卡片

**改**:
1. `components/cards/MyCard.vue`
2. `types/index.ts` 加 ChatMessageType
3. `composables/useTaskEventReducer.ts` 加事件处理（hydrate/live 共用）
4. `utils/conversationTimeline.ts` 如需新排序规则

### 22.6 我要调整 Retry

**改**:
1. `agent/retry_policy.py` 的 `RetryPolicy.decide()`
2. `tests/agent_runtime/test_retry_policy.py`

### 22.7 我要新增 Graph 版本

**改**:
1. `agent_runtime/graphs/test_plan/versions/v4/`(复制 v3 模板)
2. `graph_registry.py` 加 `"v4"` case
3. `agent_tasks.engine_type` + `graph_version` 字段(若需要)
4. 测试 `tests/agent_runtime/graphs/test_plan/versions/v4/`

**不要**修改 v2_frozen / v3 已发布版本。

### 22.8 我要新增一个 Context Engine Source(CE-05)

**改**:
1. `backend/app/context_engine/sources/my_source.py` 继承 `_base.SourceAdapter`
2. `backend/app/context_engine/sources/registry.py` 注册
3. `backend/app/context_engine/profiles/registry.py` 在对应 Profile 中引用
4. `backend/tests/context_engine/test_sources_my_source.py`

**不要**直接在 `composer.py` 写拉取逻辑,所有 Source 走 SourceAdapter 接口。

### 22.9 我要新增一个 Atomic Capability(Dynamic Agent)

**改**:
1. `backend/app/agent/atomic_capability_registry.py` 加 `CapabilityDef`
2. `backend/app/agent_runtime/dynamic_agent/tool_handlers.py` 执行 handler
3. `backend/app/agent_runtime/dynamic_agent/schemas.py` 输入输出 schema
4. `backend/tests/test_dynamic_agent_tool_integration.py` 加测试

### 22.10 我要调整 Narrative Composer 上下文压缩

**改**:
1. `backend/app/agent_runtime/narrative_composer/context_builders.py` per-Tool 压缩策略
2. `backend/app/agent_runtime/narrative_composer/validator.py` 事实约束
3. `backend/app/agent_runtime/narrative_composer/prompts.py` Prompt 调整
4. `tests/agent_runtime/test_phase29b4_narrative_composer.py` 验证

### 22.11 我要新增 Test Faults 故障类型

**改**:
1. `backend/app/test_faults/__init__.py` `_FAULT_REGISTRY` 加新故障
2. `_apply_fault` handlers 字典加分支
3. 实现具体故障函数(参考现有 5 种)
4. `backend/tests/test_fault_injection.py` 加测试

---

## 23. 新开发人员七天阅读计划(dev_3.0)

### Day 1:启动与入口(2 小时)

阅读:
- `backend/app/main.py`(lifespan 部分)
- `backend/app/core/config.py`
- `backend/app/db/session.py`
- `frontend/src/main.ts` + `App.vue` + `router/index.ts`

练习:`uvicorn app.main:app --reload` 启动 dev,`/docs` 看 OpenAPI。

### Day 2:用户与会话(2 小时)

阅读:
- `backend/app/api/v1/auth.py` + `auth_service.py`
- `backend/app/api/v1/conversations.py` + `conversation_service.py`
- `backend/app/api/v1/messages.py` + `message_service.py`
- `frontend/src/stores/authStore.ts` + `conversationStore.ts`

练习:用 curl 注册 → 登录 → 创建会话 → 发消息。

### Day 3:文件上传 + Artifact(2 小时)

阅读:
- `backend/app/api/v1/files.py` + `file_service.py` + `atomic_file_writer.py`
- `backend/app/api/v1/artifacts.py` + `artifact_service.py`
- `backend/app/services/artifact_writer.py`
- `backend/app/models/artifact.py` + `uploaded_file.py`

练习:上传文件 → 创建 Artifact → 下载 → 验证 atomic rename。

### Day 4:Legacy Orchestrator(2 小时)

阅读:
- `backend/app/agent/orchestrator.py` 主路径(`run` → `_run_tool_with_retry` → `_run_tool_once`)
- `backend/app/agent/retry_policy.py`

练习:跑一个 legacy engine_type 的 task,观察 SSE 流。

### Day 5:Agent Runtime(4 小时)

阅读:
- `backend/app/agent_runtime/api_dispatcher.py`
- `backend/app/services/agent_execution_worker.py`
- `backend/app/agent_runtime/langgraph_run_coordinator.py`
- `backend/app/agent_runtime/persistence/postgres_checkpointer.py`
- `backend/app/agent_runtime/probe_router.py`

练习:启动两个 worker,看 Outbox 领取。

### Day 6:LangGraph v3 主图 + Dynamic Agent(4 小时)

阅读:
- `agent_runtime/graphs/test_plan/state.py`
- `agent_runtime/graphs/test_plan/versions/v3/graph.py`(30+ 节点拓扑)
- 各 `nodes_*.py`(pre_confirm / post_confirm / review_format / narrative)
- `agent_runtime/graphs/dynamic_agent/graph.py`(11 节点子图)
- `agent_runtime/dynamic_agent/planner.py` + `executor.py` + `verifier.py`

练习:跑 v3 主图 preparation 子图,断点观察 state;再触发 Dynamic Agent Planner。

### Day 7:Context Engine 3.0 + Narrative Composer(4 小时)

阅读:
- `backend/app/context_engine/runtime/context_engine.py`(facade)
- `backend/app/context_engine/profiles/registry.py`(Profile Registry)
- `backend/app/context_engine/sources/knowledge.py`(Maas KB → Source)
- `backend/app/context_engine/composer/composer.py`
- `backend/app/agent_runtime/narrative_composer/composer.py` + `validator.py`
- `backend/app/agent_runtime/narrative_composer/stream_decoder.py`

练习:用一个 test_plan 任务,打开 `AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_ENABLED=1`,观察 8 Tool 卡片的 LLM 叙事 → fallback → 关闭变量回到确定性文案。

### 进阶(可选 Day 8):Test Faults 故障注入

阅读:
- `backend/app/test_faults/__init__.py` 5 种故障实现
- `backend/tests/test_fault_injection.py` 回归剧本

练习:`FAULT_ENABLED=1 FAULT_SCHEMA_HEADER_MISMATCH=1` 跑一个真实任务,看 RepairAgent → TestPlanRegenTool 链路修复。

---

**本文档结束。配套见 docs/49(总体技术方案)+ docs/51(证据索引)。**