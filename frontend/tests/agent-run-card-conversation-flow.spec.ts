import { describe, expect, it } from 'vitest'
import chatMessageListSource from '../src/components/chat/ChatMessageList.vue?raw'
import agentRunCardSource from '../src/components/cards/AgentRunCard.vue?raw'
import chatInputSource from '../src/components/chat/ChatInputBox.vue?raw'
import typesSource from '../src/types/index.ts?raw'
import messageApiSource from '../src/api/messageApi.ts?raw'
import useTaskEventsSource from '../src/composables/useTaskEvents.ts?raw'
import useTaskEventReducerSource from '../src/composables/useTaskEventReducer.ts?raw'
import eventPayloadSource from '../src/utils/eventPayload.ts?raw'

describe('agent run card conversation flow', () => {
  // ── Phase 2.9A.32 — PublicExecutionUpdate 扁平契约恢复 ──────────────
  // Phase 2.9B.6 — LLM 与 deterministic 互斥选择(同一 Tool 只显示一份叙事)。
  it('renders the PublicExecutionUpdate narrative via the single display selector', () => {
    expect(agentRunCardSource).toContain('PublicExecutionUpdate')
    expect(agentRunCardSource).toContain('displayToolUpdate(item.message)')
    expect(agentRunCardSource).toContain("item.kind === 'tool' && displayToolUpdate(item.message)")
    expect(agentRunCardSource).toContain('tool-runtime-card__details')
    expect(agentRunCardSource).toContain('tool-runtime-card__label')
    expect(agentRunCardSource).toContain('>Input</span>')
    expect(agentRunCardSource).toContain('>Output</span>')
  })

  it('does not force-render the narrative region without publicUpdate data', () => {
    // 叙事区域完全由数据驱动(displayToolUpdate 返回 undefined 时不渲染),
    // 修复前后 DOM/CSS 不变。
    expect(agentRunCardSource).toMatch(/<PublicExecutionUpdate[\s\S]*v-if="item\.kind === 'tool' && displayToolUpdate\(item\.message\)/)
  })

  it('uses a mutual-exclusion display selector, never two parallel PublicExecutionUpdate', () => {
    // Phase 2.9B.6: LLM 与 deterministic 互斥 — 同一 Tool 最终只显示一份叙事。
    expect(agentRunCardSource).toContain("source === 'deterministic'")
    expect(agentRunCardSource).toContain("source === 'llm'")
    expect(agentRunCardSource).toContain('displayToolUpdate')
    // 不再存在「两个独立 v-if」的双渲染结构。
    expect(agentRunCardSource).not.toContain(':update="item.message.toolCall.publicUpdate"')
  })

  it('useTaskEvents and useTaskEventReducer both reuse the canonical extractor', () => {
    expect(useTaskEventsSource).toContain("extractPublicExecutionUpdate(payload)")
    expect(useTaskEventsSource).not.toContain('function asPublicUpdate')
    expect(useTaskEventReducerSource).toContain("extractPublicExecutionUpdate(payload)")
    expect(useTaskEventReducerSource).not.toContain('function asPublicUpdate')
    // reducer 的 tool_started 通过 explicitChunkIndex 区分「字段缺失」与「值为 0」,
    // 不再把缺失 chunk_index 默认成 0(否则 chunk0 完成帧会被误去重)。
    expect(useTaskEventReducerSource).toContain('function explicitChunkIndex')
    expect(useTaskEventReducerSource).toContain('chunkIndex !== null ? { [chunkIndex]: true } : {}')
    expect(useTaskEventReducerSource).not.toContain('const chunkIndex = Number(payload.chunk_index ?? payload.chunkIndex ?? 0)\n  const startedMs')
  })

  it('eventPayload defines the single canonical PublicExecutionUpdate parser', () => {
    expect(eventPayloadSource).toContain('export function extractPublicExecutionUpdate')
    expect(eventPayloadSource).toContain('mergePublicExecutionUpdate')
    expect(eventPayloadSource).toContain('normalizeTaskEventPayload')
  })
  it('groups task-scoped agent events into one Agent Run card', () => {
    expect(chatMessageListSource).toContain('AgentRunCard')
    expect(chatMessageListSource).toContain('conversationItems')
    // Phase 2.9A.30: uses assembler instead of scanning
    expect(chatMessageListSource).toContain('assembleConversationTimeline')
    expect(chatMessageListSource).toContain("item.kind === 'agent-run'")
  })

  it('uses assembler for timeline instead of manual scanning', () => {
    // Phase 2.9A.30: old scanning functions removed, assembler used
    expect(chatMessageListSource).toContain('assembleConversationTimeline')
    expect(chatMessageListSource).toContain('taskRunBlocks')
  })

  it('uses the lightweight task process structure', () => {
    expect(agentRunCardSource).toContain('task-duration-header')
    expect(agentRunCardSource).toContain('task-process')
    expect(agentRunCardSource).toContain('agent-run-timeline')
    expect(agentRunCardSource).toContain('timeline-line')
    expect(agentRunCardSource).toContain('tool-runtime-card')
    expect(agentRunCardSource).toContain('artifact-result-grid')
    expect(agentRunCardSource).toContain('toolMessages')
    expect(agentRunCardSource).toContain('artifactMessages')
    expect(agentRunCardSource).toContain("'download-artifact'")
    expect(agentRunCardSource).toContain("'confirm-section'")
  })

  it('places the LLM completion summary between the collapsed run and final result cards', () => {
    expect(agentRunCardSource).toContain('task-completion-summary')
    expect(agentRunCardSource).toContain('completionSummary')
    expect(agentRunCardSource).toMatch(/agent-run-card__collapsed[\s\S]*task-completion-summary[\s\S]*artifact-result-grid/)
  })

  it('does not reveal future run steps after the first active timeline item', () => {
    expect(agentRunCardSource).toContain('revealSequentialTimelineItems')
    expect(agentRunCardSource).toContain("item.status === 'running' || item.status === 'waiting'")
    // BUG FIX 2026-08-18 (方案 1):失败时**不再**按第一个 failed 截断时间线。
    // 降级失败(如知识库 not_configured / api_unavailable)不是停止点,
    // 任务会继续执行;按 failed 截断会隐藏后续真实执行与真实失败点
    // (如 WordExportTool)。任务失败时显示全部 items。
    expect(agentRunCardSource).not.toContain("item.status === 'failed'")
    expect(agentRunCardSource).toContain('return items.slice(0, activeIndex + 1)')
  })

  it('shows task understanding and the generated execution plan inside completed planning steps', () => {
    expect(agentRunCardSource).toContain('understanding-result-panel')
    expect(agentRunCardSource).toContain('execution-plan-panel')
    expect(agentRunCardSource).toContain('理解结果')
    expect(agentRunCardSource).toContain('执行计划')
    expect(agentRunCardSource).toContain('understandingResultText')
    expect(agentRunCardSource).toContain('planDetailSteps')
    expect(agentRunCardSource).toContain('step.detail')
    expect(agentRunCardSource).toContain('plan-step-status')
  })

  it('restores the Stitch template identification and section strategy confirmation design', () => {
    expect(agentRunCardSource).toContain('template-identified-row')
    expect(agentRunCardSource).toContain('section-strategy-node')
    expect(agentRunCardSource).toContain('section-strategy-table')
    expect(agentRunCardSource).toContain('section-strategy-stat')
    expect(agentRunCardSource).toContain('section-strategy-actions')
    expect(agentRunCardSource).toContain('<AppSelect')
    expect(agentRunCardSource).toContain('confirmed-section-summary')

    expect(agentRunCardSource).toContain('max-height: 260px')
    expect(agentRunCardSource).toContain('overflow: auto')
    expect(agentRunCardSource).not.toContain('更换模板')
    expect(agentRunCardSource).not.toContain('已识别文件模板')
    expect(agentRunCardSource).toContain('确认章节处理策略')
    expect(agentRunCardSource).toContain('全部 AI 生成')
    expect(agentRunCardSource).not.toContain('全部保留原文')
    expect(agentRunCardSource).toContain('恢复建议')
    expect(agentRunCardSource).toContain('确认章节策略')
    expect(agentRunCardSource).not.toContain('返回修改模板')
  })

  it('updates a single section row by row index instead of possibly duplicated section id', () => {
    expect(agentRunCardSource).toContain('v-for="(section, index) in editableSections"')
    expect(agentRunCardSource).toContain('@update:model-value="handleSectionActionChange(index, $event)"')
    expect(agentRunCardSource).toContain('currentIndex === index')
  })

  it('deduplicates section code from section title for display only', () => {
    expect(agentRunCardSource).toContain('displaySectionName(section)')
    expect(agentRunCardSource).toContain('function displaySectionName')
    expect(agentRunCardSource).not.toContain('{{ section.code }} {{ section.title }}')
  })

  it('keeps section confirmation submit disabled until the backend task state is ready', () => {
    expect(chatMessageListSource).toContain('confirmSectionsReady')
    expect(agentRunCardSource).toContain('confirmSectionsReady')
    expect(agentRunCardSource).toContain('canSubmitSectionConfirmation')
    expect(agentRunCardSource).toContain('!canSubmitSectionConfirmation')
  })

  it('does not alter the bottom chat input component for this change', () => {
    expect(chatInputSource).toContain('class="input-dock"')
    expect(chatInputSource).toContain("emit('send'")
    expect(chatInputSource).not.toContain('AgentRunCard')
  })

  it('keeps the bottom input dock above agent timeline markers', () => {
    expect(agentRunCardSource).toContain('.timeline-marker')
    expect(agentRunCardSource).toContain('z-index: 1')
    expect(chatInputSource).toContain('z-index: 10')
  })

  // F023 Step 6 — retry progress surfaces in the UI
  it('declares retrying event type and ToolRetryProgress interface in types', () => {
    expect(typesSource).toContain("'retrying'")
    expect(typesSource).toContain('ToolRetryProgress')
    expect(typesSource).toContain("'tool_retry'")
  })

  it('maps RETRYING SSE events into tool_retry chat messages', () => {
    expect(useTaskEventsSource).toContain("retrying: 'tool_retry'")
    expect(useTaskEventsSource).toContain("case 'tool_retry'")
    expect(useTaskEventsSource).toContain('ToolRetryProgress')
  })

  it('keeps retry progress inside the active AgentRunCard instead of splitting the run', () => {
    // Phase 2.9A.30: assembler handles grouping, no more taskRunMessageTypes scanning
    expect(chatMessageListSource).toContain('assembleConversationTimeline')
    expect(chatMessageListSource).not.toContain('tool-retry-inline')
    expect(agentRunCardSource).toContain('buildRetryToolMessage')
    expect(agentRunCardSource).toContain('retryTimelineStatus')
    expect(agentRunCardSource).toContain('retryAttempt')
    expect(agentRunCardSource).toContain('tool-runtime-card__details')
  })

  it('passes TaskRunBlock timing into AgentRunCard instead of relying on latestTask summary', () => {
    expect(chatMessageListSource).toContain(':task="taskForTaskId(item.taskId, item.block)"')
    expect(chatMessageListSource).toContain('startedAt: block.startedAt ?? task?.startedAt ?? null')
    expect(chatMessageListSource).toContain('completedAt: block.completedAt ?? task?.completedAt ?? null')
    expect(chatMessageListSource).toContain('durationMs: block.durationMs ?? task?.durationMs ?? null')
  })

  // F026 — KB direct-answer attribution propagates to the chat UI
  it('keeps every tool log expanded by default until its Logs button is clicked', () => {
    expect(agentRunCardSource).toContain('openToolLogIds')
    expect(agentRunCardSource).toContain('openToolLogIds.value.has(messageId)')
    expect(agentRunCardSource).toContain('toggleToolLog')
    expect(agentRunCardSource).not.toContain('defaultOpenToolId')
    expect(agentRunCardSource).not.toContain('expandedToolId')
  })

  it('renders retry failure details only in the tool log output row', () => {
    expect(agentRunCardSource).toContain('toolLogOutput')
    expect(agentRunCardSource).toContain('tool-runtime-card__value')
    expect(agentRunCardSource).not.toContain('retry-error-line')
    expect(agentRunCardSource).not.toContain('上次失败：')
  })

  it('keeps failed tool presentation neutral outside the red FAILED status text', () => {
    expect(agentRunCardSource).not.toContain('border-color: var(--ta-error);')
    expect(agentRunCardSource).not.toContain('color: #dc2626;')
    expect(agentRunCardSource).toContain('.tool-runtime-card--failed .tool-runtime-card__status')
    expect(agentRunCardSource).toContain('.retry-badge--exhausted {\n  background: #f3f4f6;')
  })

  it('does not render raw material-symbol text for retry and confirmed-section rows', () => {
    expect(agentRunCardSource).not.toContain('>autorenew</span>')
    expect(agentRunCardSource).not.toContain('>check_circle</span>')
    expect(agentRunCardSource).not.toContain('retry-badge-spin')
  })

  it('extracts structured tool failure details into the Output row', () => {
    expect(useTaskEventsSource).toContain('formatToolFailureOutput')
    expect(useTaskEventsSource).toContain("['details', 'detail', 'message']")
  })

  it('declares KbDirectAnswer and KbSourceAttribution interfaces in types', () => {
    expect(typesSource).toContain('KbDirectAnswer')
    expect(typesSource).toContain('KbSourceAttribution')
    expect(typesSource).toContain('kbDirectAnswer')
  })

  it('mapMessage / mapAgentReply copy kb_direct_answer and intent from payload', () => {
    expect(messageApiSource).toContain('kb_direct_answer')
    expect(messageApiSource).toContain('kbDirectAnswer')
    expect(messageApiSource).toContain("obj.intent === 'string'")
    expect(messageApiSource).toContain('extractKbFields')
  })

  it('renders a KbSourceBadge above MarkdownMessage when kbDirectAnswer is set', () => {
    expect(chatMessageListSource).toContain('KbSourceBadge')
    expect(chatMessageListSource).toContain('kbDirectAnswer')
    expect(chatMessageListSource).toMatch(/KbSourceBadge[\s\S]*kbDirectAnswer/)
  })

  // F026 — KB direct-answer attribution propagates to the chat UI
  it('keeps every tool log expanded by default until its Logs button is clicked', () => {
    expect(agentRunCardSource).toContain('openToolLogIds')
    expect(agentRunCardSource).toContain('openToolLogIds.value.has(messageId)')
    expect(agentRunCardSource).toContain('toggleToolLog')
    expect(agentRunCardSource).not.toContain('defaultOpenToolId')
    expect(agentRunCardSource).not.toContain('expandedToolId')
  })

  it('renders retry failure details only in the tool log output row', () => {
    expect(agentRunCardSource).toContain('toolLogOutput')
    expect(agentRunCardSource).toContain('tool-runtime-card__value')
    expect(agentRunCardSource).not.toContain('retry-error-line')
    expect(agentRunCardSource).not.toContain('上次失败：')
  })

  it('keeps failed tool presentation neutral outside the red FAILED status text', () => {
    expect(agentRunCardSource).not.toContain('border-color: var(--ta-error);')
    expect(agentRunCardSource).not.toContain('color: #dc2626;')
    expect(agentRunCardSource).toContain('.tool-runtime-card--failed .tool-runtime-card__status')
    expect(agentRunCardSource).toContain('.retry-badge--exhausted {\n  background: #f3f4f6;')
  })

  it('does not render raw material-symbol text for retry and confirmed-section rows', () => {
    expect(agentRunCardSource).not.toContain('>autorenew</span>')
    expect(agentRunCardSource).not.toContain('>check_circle</span>')
    expect(agentRunCardSource).not.toContain('retry-badge-spin')
  })

  it('extracts structured tool failure details into the Output row', () => {
    expect(useTaskEventsSource).toContain('formatToolFailureOutput')
    expect(useTaskEventsSource).toContain("['details', 'detail', 'message']")
  })

  it('declares KbDirectAnswer and KbSourceAttribution interfaces in types', () => {
    expect(typesSource).toContain('KbDirectAnswer')
    expect(typesSource).toContain('KbSourceAttribution')
    expect(typesSource).toContain('kbDirectAnswer')
  })

  it('mapMessage / mapAgentReply copy kb_direct_answer and intent from payload', () => {
    expect(messageApiSource).toContain('kb_direct_answer')
    expect(messageApiSource).toContain('kbDirectAnswer')
    expect(messageApiSource).toContain("obj.intent === 'string'")
    expect(messageApiSource).toContain('extractKbFields')
  })

  it('renders a KbSourceBadge above MarkdownMessage when kbDirectAnswer is set', () => {
    expect(chatMessageListSource).toContain('KbSourceBadge')
    expect(chatMessageListSource).toContain('kbDirectAnswer')
    expect(chatMessageListSource).toMatch(/KbSourceBadge[\s\S]*kbDirectAnswer/)
  })

  // F026 — KB direct-answer attribution propagates to the chat UI
  it('keeps every tool log expanded by default until its Logs button is clicked', () => {
    expect(agentRunCardSource).toContain('openToolLogIds')
    expect(agentRunCardSource).toContain('openToolLogIds.value.has(messageId)')
    expect(agentRunCardSource).toContain('toggleToolLog')
    expect(agentRunCardSource).not.toContain('defaultOpenToolId')
    expect(agentRunCardSource).not.toContain('expandedToolId')
  })

  it('renders retry failure details only in the tool log output row', () => {
    expect(agentRunCardSource).toContain('toolLogOutput')
    expect(agentRunCardSource).toContain('tool-runtime-card__value')
    expect(agentRunCardSource).not.toContain('retry-error-line')
    expect(agentRunCardSource).not.toContain('上次失败：')
  })

  it('keeps failed tool presentation neutral outside the red FAILED status text', () => {
    expect(agentRunCardSource).not.toContain('border-color: var(--ta-error);')
    expect(agentRunCardSource).not.toContain('color: #dc2626;')
    expect(agentRunCardSource).toContain('.tool-runtime-card--failed .tool-runtime-card__status')
    expect(agentRunCardSource).toContain('.retry-badge--exhausted {\n  background: #f3f4f6;')
  })

  it('does not render raw material-symbol text for retry and confirmed-section rows', () => {
    expect(agentRunCardSource).not.toContain('>autorenew</span>')
    expect(agentRunCardSource).not.toContain('>check_circle</span>')
    expect(agentRunCardSource).not.toContain('retry-badge-spin')
  })

  it('extracts structured tool failure details into the Output row', () => {
    expect(useTaskEventsSource).toContain('formatToolFailureOutput')
    expect(useTaskEventsSource).toContain("['details', 'detail', 'message']")
  })

  it('declares KbDirectAnswer and KbSourceAttribution interfaces in types', () => {
    expect(typesSource).toContain('KbDirectAnswer')
    expect(typesSource).toContain('KbSourceAttribution')
    expect(typesSource).toContain('kbDirectAnswer')
  })

  it('mapMessage / mapAgentReply copy kb_direct_answer and intent from payload', () => {
    expect(messageApiSource).toContain('kb_direct_answer')
    expect(messageApiSource).toContain('kbDirectAnswer')
    expect(messageApiSource).toContain("obj.intent === 'string'")
    expect(messageApiSource).toContain('extractKbFields')
  })

  it('renders a KbSourceBadge above MarkdownMessage when kbDirectAnswer is set', () => {
    expect(chatMessageListSource).toContain('KbSourceBadge')
    expect(chatMessageListSource).toContain('kbDirectAnswer')
    expect(chatMessageListSource).toMatch(/KbSourceBadge[\s\S]*kbDirectAnswer/)
  })

  // ── Phase 2.9B.2 — 动态叙事独立渲染路径 ──────────────────────────────
  it('wires dynamicNarratives from TaskRunBlock into AgentRunCard', () => {
    // ChatMessageList 把 block.dynamicNarratives 传给 AgentRunCard。
    expect(chatMessageListSource).toContain(':dynamic-narratives="item.block?.dynamicNarratives ?? []"')
    // AgentRunCard 声明动态叙事 prop。
    expect(agentRunCardSource).toContain('dynamicNarratives?: DynamicNarrative[]')
  })

  it('renders dynamic narratives via the existing PublicExecutionUpdate component', () => {
    // 复用现有叙事组件, 不新建一套 UI。
    expect(agentRunCardSource).toContain("item.kind === 'narrative' && item.narrative?.publicUpdate")
    expect(agentRunCardSource).toContain(':update="item.narrative.publicUpdate"')
  })

  it('places dynamic narratives at their real event position, not the task bottom', () => {
    // 叙事节点由 canonicalOrder / sequenceNo 锚定到工具之后(§15)。
    // BUG FIX 2026-08-19:锚定逻辑抽到 utils/timelineAnchor 后,AgentRunCard
    // 仍调用 insertAtOrder helper 并保留对 narrative 形态的引用。
    expect(agentRunCardSource).toContain('kind: \'narrative\'')
    expect(agentRunCardSource).toContain('insertAtOrder')
    expect(agentRunCardSource).toContain('timelineAnchorInsertAtOrder')
  })

  it('does not render dynamic narratives inside Tool Logs', () => {
    // 叙事不进入 tool-log-panel(叙事行不引用 toolLogOutput)。
    const narrativeRender = agentRunCardSource.match(/item\.kind === 'narrative'[\s\S]{0,300}/)?.[0] ?? ''
    expect(narrativeRender).not.toContain('tool-log-panel')
    expect(narrativeRender).not.toContain('toolLogOutput')
  })

  it('keeps AgentRunCard existing layout and styles unchanged for dynamic narratives', () => {
    // 未新增 CSS class / 卡片包装 / 视觉样式。
    expect(agentRunCardSource).not.toContain('dynamic-narrative-card')
    expect(agentRunCardSource).not.toContain('dynamic-narrative__')
    expect(agentRunCardSource).not.toContain('--ta-dynamic')
    // 既有 2.9A 工具叙事仍存在(经互斥选择器)。
    expect(agentRunCardSource).toContain("item.kind === 'tool' && displayToolUpdate(item.message)")
  })

  // F026 — KB direct-answer attribution propagates to the chat UI
  it('keeps every tool log expanded by default until its Logs button is clicked', () => {
    expect(agentRunCardSource).toContain('openToolLogIds')
    expect(agentRunCardSource).toContain('openToolLogIds.value.has(messageId)')
    expect(agentRunCardSource).toContain('toggleToolLog')
    expect(agentRunCardSource).not.toContain('defaultOpenToolId')
    expect(agentRunCardSource).not.toContain('expandedToolId')
  })

  it('renders retry failure details only in the tool log output row', () => {
    expect(agentRunCardSource).toContain('toolLogOutput')
    expect(agentRunCardSource).toContain('tool-runtime-card__value')
    expect(agentRunCardSource).not.toContain('retry-error-line')
    expect(agentRunCardSource).not.toContain('上次失败：')
  })

  it('keeps failed tool presentation neutral outside the red FAILED status text', () => {
    expect(agentRunCardSource).not.toContain('border-color: var(--ta-error);')
    expect(agentRunCardSource).not.toContain('color: #dc2626;')
    expect(agentRunCardSource).toContain('.tool-runtime-card--failed .tool-runtime-card__status')
    expect(agentRunCardSource).toContain('.retry-badge--exhausted {\n  background: #f3f4f6;')
  })

  it('does not render raw material-symbol text for retry and confirmed-section rows', () => {
    expect(agentRunCardSource).not.toContain('>autorenew</span>')
    expect(agentRunCardSource).not.toContain('>check_circle</span>')
    expect(agentRunCardSource).not.toContain('retry-badge-spin')
  })

  it('extracts structured tool failure details into the Output row', () => {
    expect(useTaskEventsSource).toContain('formatToolFailureOutput')
    expect(useTaskEventsSource).toContain("['details', 'detail', 'message']")
  })

  it('declares KbDirectAnswer and KbSourceAttribution interfaces in types', () => {
    expect(typesSource).toContain('KbDirectAnswer')
    expect(typesSource).toContain('KbSourceAttribution')
    expect(typesSource).toContain('kbDirectAnswer')
  })

  it('mapMessage / mapAgentReply copy kb_direct_answer and intent from payload', () => {
    expect(messageApiSource).toContain('kb_direct_answer')
    expect(messageApiSource).toContain('kbDirectAnswer')
    expect(messageApiSource).toContain("obj.intent === 'string'")
    expect(messageApiSource).toContain('extractKbFields')
  })

  it('renders a KbSourceBadge above MarkdownMessage when kbDirectAnswer is set', () => {
    expect(chatMessageListSource).toContain('KbSourceBadge')
    expect(chatMessageListSource).toContain('kbDirectAnswer')
    expect(chatMessageListSource).toMatch(/KbSourceBadge[\s\S]*kbDirectAnswer/)
  })
})
