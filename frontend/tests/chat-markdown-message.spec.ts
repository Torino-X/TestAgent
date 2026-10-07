import { describe, expect, it } from 'vitest'
import chatMessageListSource from '../src/components/chat/ChatMessageList.vue?raw'
import markdownMessageSource from '../src/components/chat/MarkdownMessage.vue?raw'

describe('chat markdown message rendering', () => {
  it('renders ordinary agent text through MarkdownMessage', () => {
    expect(chatMessageListSource).toContain('MarkdownMessage')
    expect(chatMessageListSource).toContain('isMarkdownAgentText')
    expect(chatMessageListSource).toContain("message.type === 'agent_text'")
    expect(chatMessageListSource).toContain(':content="item.message.text ??')
  })

  it('keeps task-scoped agent text inside the agent run card instead of markdown bubbles', () => {
    // Phase 2.9A.30: assembler handles grouping, no more inline task checks
    expect(chatMessageListSource).toContain('assembleConversationTimeline')
    expect(chatMessageListSource).toContain('AgentRunCard')
  })

  it('renders ordinary assistant replies as headerless document content', () => {
    expect(chatMessageListSource).toContain('message-row--assistant')
    expect(chatMessageListSource).toContain('thinking-status')
    expect(chatMessageListSource).toContain('message-actions')
    expect(chatMessageListSource).not.toContain('class="agent-label"')
    expect(chatMessageListSource).not.toContain('SparklesOutline')
    expect(markdownMessageSource).toContain('assistant-content')
    expect(markdownMessageSource).toContain('padding: 0')
    expect(markdownMessageSource).toContain('margin: 0')
    expect(markdownMessageSource).not.toContain('markdown-message')
    expect(markdownMessageSource).not.toContain('background: var(--ta-surface)')
  })
})
