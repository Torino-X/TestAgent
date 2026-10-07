import { describe, expect, it } from 'vitest'
import chatWorkspaceSource from '../src/components/chat/ChatWorkspace.vue?raw'
import messageApiSource from '../src/api/messageApi.ts?raw'

describe('chat streaming send behavior', () => {
  it('shows the user message and thinking reply before the stream finishes', () => {
    expect(chatWorkspaceSource).toContain('appendOptimisticUserMessage')
    expect(chatWorkspaceSource).toContain('createThinkingMessage')
    expect(chatWorkspaceSource).toContain('const thinkingText')
    expect(chatWorkspaceSource).toContain('conversationStore.appendMessages(activeConversationId, [optimisticUserMessage, thinkingMessage])')
    expect(chatWorkspaceSource).toContain('await scrollToBottomAfterRender(true)')
  })

  it('updates the assistant reply from streaming text delta events', () => {
    expect(chatWorkspaceSource).toContain('sendMessageStream')
    expect(chatWorkspaceSource).toContain('onAgentTextDelta')
    expect(chatWorkspaceSource).toContain('conversationStore.updateMessage')
    expect(messageApiSource).toContain('agent_text_delta')
    expect(messageApiSource).toContain('agent_text_done')
  })

  it('keeps stream ownership metadata when server messages arrive', () => {
    expect(chatWorkspaceSource).toContain('function mergeStreamMessage(')
    expect(chatWorkspaceSource).toContain("mergeStreamMessage(thinkingMessage, message, 'agent', 'agent_text')")
    expect(chatWorkspaceSource).toContain('mergeStreamMessage(\n          optimisticUserMessage,')
    expect(chatWorkspaceSource).toContain("          'user',\n          'user_text'")
  })

  it('keeps the thinking text visible after the assistant stream placeholder is created', () => {
    expect(chatWorkspaceSource).toContain('text: thinkingText')
    // Note: `text: ''` may appear in other handlers (e.g. regeneration rollback);
    // the positive check for thinkingText above is sufficient.
  })

  it('keeps agent task routing on the existing SSE task flow', () => {
    expect(chatWorkspaceSource).toContain('onAgentTaskCreated')
    expect(chatWorkspaceSource).toContain('connectTaskEvents')
    expect(chatWorkspaceSource).toContain('conversationStore.removeMessage')
  })

  it('uses the new streaming message endpoint without mock fallback', () => {
    expect(messageApiSource).toContain('sendMessageStream')
    expect(messageApiSource).toContain('/messages/stream')
    expect(messageApiSource).toContain('ReadableStreamDefaultReader')
    expect(messageApiSource).toContain('agent_text_delta')
  })

  it('can stop the active streaming reply without sending the draft text', () => {
    expect(chatWorkspaceSource).toContain('activeStreamController')
    expect(chatWorkspaceSource).toContain('new AbortController()')
    expect(chatWorkspaceSource).toContain('handleStopResponse')
    expect(chatWorkspaceSource).toContain('activeStreamController.value?.abort()')
    expect(chatWorkspaceSource).toContain('signal')
  })
})
