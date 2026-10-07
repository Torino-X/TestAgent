import { describe, expect, it } from 'vitest'
import source from './ChatWorkspace.vue?raw'

describe('ChatWorkspace context usage refresh wiring', () => {
  it('mounts the active preparation clarification above the input dock', () => {
    expect(source).toContain('class="chat-workspace__clarification"')
    expect(source).toContain('<PreparationClarificationCard')
    expect(source).toContain('findLatestPreparationClarificationMessage')
    expect(source).toContain("--chat-composer-panel-top-offset: 160px")
    expect(source).toContain("--floating-interaction-gap: 12px")
    expect(source).toContain('bottom: calc(var(--chat-composer-panel-top-offset) + var(--floating-interaction-gap));')
    expect(source).toContain('@dock-layout="handleComposerDockLayout"')
    expect(source).toContain('() => pendingPreparationClarification.value?.id')
    expect(source).toContain('scrollToBottomAfterRender(true)')
    expect(source).toContain('@layout-change="handleClarificationCardLayoutChange"')
    expect(source).toContain("'chat-scroll--floating-confirmation'")
    expect(source).toMatch(/\.chat-scroll--floating-confirmation\s*\{[\s\S]*padding-bottom:\s*640px;/)
    expect(source).toContain('function handleClarificationCardLayoutChange(expanded: boolean)')
    expect(source).toContain('{ immediate: true }')
  })

  it('mounts the active section confirmation in the shared floating interaction area', () => {
    expect(source).toContain("import SectionConfirmCard from '@/components/cards/SectionConfirmCard.vue'")
    expect(source).toContain('pendingSectionConfirmation')
    expect(source).toContain('class="chat-workspace__section-confirmation"')
    expect(source).toContain('@confirm="handlePendingSectionConfirmation"')
    expect(source).toContain('findLatestSectionConfirmMessage')
    expect(source).toContain("'chat-scroll--floating-confirmation'")
  })

  it('replaces submitted confirmation interactions with durable compact receipts', () => {
    expect(source).toContain('buildSectionConfirmationReceipt(payload.sections)')
    expect(source).toContain('buildPreparationClarificationReceipt(')
    const sectionSubmitSource = source.slice(
      source.indexOf('async function handleConfirmSections'),
      source.indexOf('async function handlePreparationClarification')
    )
    const clarificationSubmitSource = source.slice(
      source.indexOf('async function handlePreparationClarification'),
      source.indexOf('function handlePendingPreparationClarification')
    )
    expect(sectionSubmitSource).not.toContain("type: 'agent_text'")
    expect(clarificationSubmitSource).not.toContain("type: 'agent_text'")
    expect(source).toContain('confirmationReceipt:')
  })

  it('submits an actionable confirmation card without depending on another task as latestTask', () => {
    const sectionSubmitSource = source.slice(
      source.indexOf('async function handleConfirmSections'),
      source.indexOf('async function handlePreparationClarification')
    )

    expect(sectionSubmitSource).toContain('findConversationMessage(activeConversation, payload.messageId)')
    expect(sectionSubmitSource).toContain("sourceMessage?.type !== 'section_confirm'")
    expect(sectionSubmitSource).toContain('sourceMessage.taskId !== payload.taskId')
    expect(sectionSubmitSource).not.toContain("task?.task_id !== payload.taskId || task.status !== 'waiting_user_confirm'")
  })

  it('refreshes usage on conversation switch, chat completion, popover open, compact success, and agent terminal status', () => {
    expect(source).toContain("contextUsage.refresh('conversation-switch')")
    expect(source).toContain("contextUsage.refresh('chat-completed')")
    expect(source).toContain("contextUsage.refresh('popover-open')")
    expect(source).toContain("contextUsage.compactContext()")
    expect(source).toContain("contextUsage.refresh('agent-task-terminal')")
    expect(source).toContain("getConversationId: () => props.conversation.id === 'conv_new' ? null : props.conversation.id")
    expect(source).toContain('{ immediate: true }')
  })

  it('initializes the auto SSE connection guard before immediate conversation watchers run', () => {
    const guardDeclaration = source.indexOf("const _autoSseConnectedTaskId = ref('')")
    const conversationSwitchRefresh = source.indexOf("contextUsage.refresh('conversation-switch')")

    expect(guardDeclaration).toBeGreaterThanOrEqual(0)
    expect(conversationSwitchRefresh).toBeGreaterThanOrEqual(0)
    expect(guardDeclaration).toBeLessThan(conversationSwitchRefresh)
  })

  it('reconnects a non-terminal task after recovering from an SSE disconnect', () => {
    expect(source).toContain('function reconnectTaskEventsIfStillActive(conversationId: string, taskId: string)')
    expect(source).toContain('.finally(() => reconnectTaskEventsIfStillActive(conversationId, taskId))')
    expect(source).toContain('|| terminalTaskStatuses.has(currentTask.status)')
    expect(source).toContain('|| sse.isReconnecting.value')
    expect(source).toContain('sse.reconnect()')
  })

  it('creates a real conversation before opening the ordinary message stream', () => {
    const handleSend = source.indexOf('async function handleSend(text: string)')
    const clearActiveStream = source.indexOf('function clearActiveStream()', handleSend)
    const handleSendSource = source.slice(handleSend, clearActiveStream)
    const ensureConversation = handleSendSource.indexOf(
      'const activeConversation = await conversationStore.ensureActiveConversation(props.conversation.id)'
    )
    const assignConversationId = handleSendSource.indexOf('activeConversationId = activeConversation.id', ensureConversation)
    const sendStream = handleSendSource.indexOf('await messageApi.sendMessageStream(activeConversationId, text', assignConversationId)

    expect(handleSend).toBeGreaterThanOrEqual(0)
    expect(clearActiveStream).toBeGreaterThan(handleSend)
    expect(ensureConversation).toBeGreaterThanOrEqual(0)
    expect(assignConversationId).toBeGreaterThan(ensureConversation)
    expect(sendStream).toBeGreaterThan(assignConversationId)
    expect(handleSendSource).not.toContain('messageApi.sendMessageStream(activeConversation.id, text')
  })

  it('releases the direct-chat lifecycle before processing the terminal payload', () => {
    const terminalStart = source.indexOf('onAgentTextDone: (result) => {')
    const taskStart = source.indexOf('onAgentTaskCreated:', terminalStart)
    const terminalSource = source.slice(terminalStart, taskStart)

    expect(terminalStart).toBeGreaterThanOrEqual(0)
    expect(terminalSource.indexOf('finishDirectReply(controller)')).toBeGreaterThanOrEqual(0)
    expect(terminalSource.indexOf('finishDirectReply(controller)')).toBeLessThan(
      terminalSource.indexOf('if (!result.agent_reply)')
    )
  })

  it('awaits cancellation of active work before starting a replacement message stream', () => {
    const handleSend = source.indexOf('async function handleSend(text: string)')
    const sendStream = source.indexOf('await messageApi.sendMessageStream(', handleSend)
    const handleSendSource = source.slice(handleSend, sendStream)

    expect(handleSendSource).toContain('await stopActiveWork()')
    expect(source).toContain('const cancelledTask = await agentApi.cancelTask(')
    expect(source).toContain("conversationStore.updateLatestTaskStatus(conversationId, 'cancelled'")
  })

  it('turns a pre-event stream timeout into an explicit retryable error', () => {
    const watchdogStart = source.indexOf('streamWatchdogTimer = setTimeout(() => {')
    const watchdogEnd = source.indexOf('}, STREAM_IDLE_TIMEOUT_MS)', watchdogStart)
    const watchdogSource = source.slice(watchdogStart, watchdogEnd)

    expect(watchdogStart).toBeGreaterThanOrEqual(0)
    expect(watchdogSource).toContain("type: 'error'")
    expect(watchdogSource).toContain("text: '响应超时，请重试'")
    expect(watchdogSource).toContain('thinking: false')
    expect(watchdogSource).toContain('streaming: false')
  })

  it('uses controller identity plus authoritative timeline reconciliation for direct replies', () => {
    expect(source).toContain('function isCurrentDirectReply(controller: AbortController): boolean')
    expect(source).toContain('function finishDirectReply(controller: AbortController): boolean')
    expect(source).toContain('async function reconcileDirectReplyAfterTransportEnd(')
      expect(source).toContain('function scheduleDirectReplyPersistenceCheck(')
      expect(source).toContain('const persistedMessages = await messageApi.fetchMessages(input.conversationId)')
      expect(source).toContain('if (!isCurrentDirectReply(input.controller)) return')
      expect(source).toContain('if (attempt < 4) scheduleDirectReplyPersistenceCheck(input, attempt + 1)')
    expect(source).toContain('transportCompletedNormally')
    expect(source).not.toContain('let directReplyRunSequence = 0')
    expect(source).not.toContain('let activeDirectReplyRunId = 0')
    expect(source).toContain('function finalizeDirectReplyMessage(')
    expect(source).toContain('optimistic: false')

    const doneStart = source.indexOf('onAgentTextDone: (result) => {')
    const taskStart = source.indexOf('onAgentTaskCreated:', doneStart)
    const doneSource = source.slice(doneStart, taskStart)

    expect(doneSource).toContain('receivedTerminalEvent = true')
    expect(doneSource).toContain('finishDirectReply(controller)')
    expect(doneSource).toContain('finalizeDirectReplyMessage(activeConversationId, agentMessageId, result.agent_reply)')
    expect(doneSource).toContain('isCurrentDirectReply(controller)')
  })

  it('keeps following streamed content through post-render layout changes', () => {
    expect(source).toContain('new ResizeObserver(() => scheduleFollowToBottom())')
    expect(source).toContain('requestAnimationFrame(() => {')
    expect(source).toContain('scrollEl.scrollTop = scrollEl.scrollHeight - scrollEl.clientHeight')
    expect(source).toContain('await nextTick()')
  })
})
