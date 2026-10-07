import { describe, expect, it } from 'vitest'
import chatWorkspaceSource from '../src/components/chat/ChatWorkspace.vue?raw'

describe('chat workspace scroll behavior', () => {
  it('forces the latest user message into view after sending', () => {
    expect(chatWorkspaceSource).toContain('ref="chatScrollRef"')
    expect(chatWorkspaceSource).toContain('@scroll="handleScroll"')
    expect(chatWorkspaceSource).toContain('await scrollToBottomAfterRender(true)')
  })

  it('only follows incoming agent stream messages while the user is near the bottom', () => {
    expect(chatWorkspaceSource).toContain('const shouldScroll = shouldFollowIncomingMessages.value')
    expect(chatWorkspaceSource).toContain('scrollToBottomAfterRender(shouldScroll)')
    expect(chatWorkspaceSource).toContain('function isNearBottom')
    expect(chatWorkspaceSource).toContain('shouldFollowIncomingMessages.value = isNearBottom()')
  })

  it('keeps streaming run content clear of the fixed input dock', () => {
    expect(chatWorkspaceSource).toContain('--chat-input-safe-area')
    expect(chatWorkspaceSource).toContain('padding: 32px 24px var(--chat-input-safe-area)')
    expect(chatWorkspaceSource).toContain('width: min(100%, var(--ta-wide-card-width))')
    expect(chatWorkspaceSource).toContain('margin: 0 auto')
    expect(chatWorkspaceSource).not.toContain('bottom: calc(var(--chat-input-safe-area) - 156px)')
  })

  it('brings a chapter confirmation card above the fixed input dock', () => {
    expect(chatWorkspaceSource).toContain("message.type === 'section_confirm'")
    expect(chatWorkspaceSource).toContain('scrollToBottomAfterRender(shouldScroll || message.type === \'section_confirm\')')
    expect(chatWorkspaceSource).toContain('scroll-padding-bottom: var(--chat-input-safe-area)')
  })
})
