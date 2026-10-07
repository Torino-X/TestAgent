# AGENTS.md

## 项目说明

当前目录是 TestAgent 前端项目目录。

TestAgent 是一个 Web 端 AI Agent 测试文档生成工作台。第一阶段只聚焦「测试方案生成 Agent MVP」。

------

## 当前任务优先级

当前任务是将 Stitch 生成的 UI 设计稿，还原为真实可运行的 Vue 前端项目。

本任务是 **前端 UI 还原任务**，不是后端集成任务。

------

## 前端技术栈要求

只能使用以下前端技术栈：

- Vue 3
- TypeScript
- Vite
- Naive UI
- Pinia
- Vue Router
- Axios，仅作为后续接口占位使用
- SSE / EventSource，仅作为后续 Agent 事件流占位使用

禁止使用：

- React
- Next.js
- Element Plus
- Ant Design Vue
- jQuery
- Bootstrap
- Tailwind，除非项目已经明确选择使用
- 不必要的大型动画库或重型依赖

------

## 设计稿来源

主要设计来源是：

```text
../stitch-export/TestAgent-AI-automation-platform/
```

请将 Stitch 导出的 HTML、截图、图片、间距、颜色、字体、卡片、布局和视觉层级作为第一优先级参考。

前端实现应尽可能接近 Stitch 设计稿的视觉效果。

------

## 产品文档

以下文档作为功能和交互上下文：

- `../docs/TestAgent_页面与交互说明文档.md`
- `../docs/TestAgent_功能清单.md`
- `../docs/TestAgent_产品需求文档_PRD.md`
- `../docs/TestAgent_技术栈选型确认.md`

其他文档只作为背景资料。除非明确要求，否则不要让其他文档影响本轮 UI 还原任务。

------

## 当前实现范围

本轮只实现前端 UI 页面和组件。

需要实现以下页面和状态：

1. 登录页
2. 会话工作台空状态
3. 上传文件后的会话工作台状态
4. Agent 执行计划状态
5. 工具调用过程状态
6. 章节确认卡片状态
7. 生成中状态
8. 审查结果状态
9. 产物下载状态
10. 历史会话恢复状态
11. 设置页

------

## 重要交互组件

需要实现以下可复用组件：

- Sidebar
- ConversationList
- ChatWorkspace
- ChatMessageList
- ChatInputBox
- FileAttachmentCard
- AgentPlanCard
- ToolCallMessage
- RequirementSummaryCard
- TemplateSummaryCard
- KnowledgeSearchSummaryCard
- SectionConfirmCard
- GeneratingStatusCard
- ReviewResultCard
- ArtifactDownloadCard
- ErrorMessageCard
- SettingsForm

------

## 数据策略

当前阶段使用 mock 数据。

本轮任务不连接真实后端接口。

mock 数据统一放在：

```text
src/mocks/
```

接口占位模块统一放在：

```text
src/api/
```

`src/api/` 中的模块可以暴露函数，但当前阶段只能返回 mock 数据。

------

## 实现规则

1. 不要修改 `stitch-export` 目录中的任何文件。
2. 不要实现后端逻辑。
3. 不要实现真实 Agent 工作流。
4. 不要实现真实文件解析。
5. 不要实现真实 Word 导出。
6. 不要实现真实登录鉴权。
7. 除非必要，不要删除已有项目文件。
8. 保持组件模块化，避免所有代码堆在一个文件中。
9. 使用 TypeScript 定义消息类型、任务状态、文件状态、章节处理方式、产物类型等核心类型。
10. UI 视觉效果必须尽量和 Stitch 截图保持一致。
11. 本轮优先保证 UI 还原质量，而不是后端功能完整性。
12. 实现完成后，确保项目可以通过 `npm install` 和 `npm run dev` 正常运行。
13. 如果新增依赖，需要说明新增原因。

------

## 验证要求

完成后需要检查：

1. 应用可以正常启动。
2. 所有要求的页面都可以访问。
3. UI 尽量匹配 Stitch 截图。
4. TypeScript 没有明显错误。
5. 浏览器控制台没有明显报错。
6. 所有核心页面状态都可以通过 mock 数据展示。
7. 没有遗留无用测试代码或临时调试输出。

------

## 本轮禁止事项

本轮禁止实现以下内容：

1. 真实后端接口调用。
2. 真实数据库逻辑。
3. 真实 Agent 编排流程。
4. 真实 SSE 长连接业务逻辑。
5. 真实文件解析。
6. 真实知识库检索。
7. 真实模型调用。
8. 真实 Word 文档生成。
9. 测试用例生成。
10. PPT 生成。
11. 自建 RAG 知识库。
12. 多 Agent 协作。
13. 复杂权限系统。

这些内容属于后续阶段，本轮只做前端 UI 静态还原和 mock 状态展示。

------

## 页面还原优先级

本轮 UI 还原优先级如下：

```text
Stitch 导出的 HTML / 截图 / 资源
↓
TestAgent 页面与交互说明文档
↓
TestAgent 技术栈选型确认文档
↓
TestAgent 功能清单
↓
TestAgent 产品需求文档 PRD
```

如果文档描述和 Stitch 视觉稿存在冲突，优先保证 Stitch 视觉稿的还原效果；如果 Stitch 缺少某些交互状态，则根据页面与交互说明文档补全。

------

## 代码组织建议

建议前端目录结构如下：

```text
frontend/
├── src/
│   ├── api/
│   ├── assets/
│   ├── components/
│   │   ├── layout/
│   │   ├── chat/
│   │   ├── cards/
│   │   └── settings/
│   ├── mocks/
│   ├── router/
│   ├── stores/
│   ├── styles/
│   ├── types/
│   ├── views/
│   ├── App.vue
│   └── main.ts
├── package.json
├── vite.config.ts
└── AGENTS.md
```

------

## 组件实现要求

### 布局组件

需要实现：

- AppLayout
- Sidebar
- ConversationList

要求：

1. 左侧会话栏需要接近 Stitch 设计稿。
2. 支持新建会话按钮。
3. 支持历史会话列表。
4. 支持设置入口。
5. 支持当前用户信息展示。

------

### 对话组件

需要实现：

- ChatWorkspace
- ChatMessageList
- UserMessage
- AgentMessage
- ChatInputBox

要求：

1. 页面整体是类 ChatGPT 的对话式工作台。
2. 用户消息和 Agent 消息需要有明显区分。
3. 底部输入框固定在对话区域底部。
4. 文件卡片显示在输入框上方。
5. 任务执行中状态要能通过 mock 数据展示。

------

### 卡片组件

需要实现：

- FileAttachmentCard
- AgentPlanCard
- ToolCallMessage
- RequirementSummaryCard
- TemplateSummaryCard
- KnowledgeSearchSummaryCard
- SectionConfirmCard
- GeneratingStatusCard
- ReviewResultCard
- ArtifactDownloadCard
- ErrorMessageCard

要求：

1. 所有卡片样式尽量贴近 Stitch。
2. 卡片之间的圆角、阴影、边框、间距要统一。
3. 章节确认卡片是核心组件，需要重点还原。
4. 审查结果卡片需要有清晰的信息摘要。
5. 产物下载卡片需要突出下载按钮。

------

### 设置页组件

需要实现：

- SettingsForm

设置页包含：

1. 模型配置；
2. API Base URL；
3. API Key；
4. 模型名称；
5. 超时时间；
6. 公司知识库 API 配置；
7. 是否启用知识库检索；
8. 文件上传大小限制；
9. 保存按钮；
10. 连接测试按钮。

设置页应保持简洁，不要做成复杂后台系统风格。

------

## TypeScript 类型要求

需要在 `src/types/` 中定义核心类型。

至少包括：

```ts
export type MessageType =
  | 'user_text'
  | 'user_file'
  | 'agent_text'
  | 'agent_plan'
  | 'tool_call'
  | 'requirement_summary'
  | 'template_summary'
  | 'knowledge_summary'
  | 'section_confirm'
  | 'generating_status'
  | 'review_result'
  | 'artifact_download'
  | 'error'

export type TaskStatus =
  | 'created'
  | 'planning'
  | 'running'
  | 'waiting_user_confirm'
  | 'generating'
  | 'reviewing'
  | 'exporting'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type FileType =
  | 'requirement_doc'
  | 'test_plan_template'
  | 'supplemental_doc'
  | 'unknown'

export type FileUploadStatus =
  | 'uploading'
  | 'uploaded'
  | 'failed'
  | 'identifying'
  | 'need_confirm'
  | 'confirmed'

export type SectionAction =
  | 'ai_generate'
  | 'keep_template'
  | 'manual_fill'
  | 'skip'

export type ArtifactType =
  | 'test_plan_word'
```

------

## Mock 数据要求

需要在 `src/mocks/` 中准备 mock 数据，用于展示以下状态：

1. 空会话；
2. 已上传文件；
3. Agent 执行计划；
4. 工具调用过程；
5. 需求解析摘要；
6. 模板解析摘要；
7. 知识库检索摘要；
8. 章节确认列表；
9. 生成中状态；
10. 审查结果；
11. 产物下载；
12. 历史会话恢复；
13. 设置页配置。

------

## 路由要求

至少实现以下路由：

```text
/login
/chat
/chat/:conversationId
/settings
```

如果需要展示不同状态，可以通过 mock 数据、查询参数或页面内切换方式展示。

------

## 运行要求

完成后必须确保：

```bash
npm install
npm run dev
```

可以正常运行。

如项目中已有其他包管理器配置，可以遵循现有项目规范，但不要随意切换技术方案。

------

## 交付说明要求

完成任务后，请说明：

1. 创建了哪些文件；
2. 修改了哪些文件；
3. 已完成哪些页面；
4. 已完成哪些组件；
5. 如何运行项目；
6. 是否有新增依赖；
7. 是否有与 Stitch 设计稿不一致的地方；
8. 哪些内容仍然是 mock；
9. 下一步建议做什么。