import { describe, expect, it } from 'vitest'
import source from './ChatView.vue?raw'

describe('ChatView placeholder conversation routing', () => {
  it('does not restore the frontend-only conv_new placeholder from the backend', () => {
    const placeholderGuard = source.indexOf("id === 'conv_new'")
    const restoreCall = source.indexOf('conversationStore.restoreConversation(id)')

    expect(placeholderGuard).toBeGreaterThanOrEqual(0)
    expect(restoreCall).toBeGreaterThan(placeholderGuard)
  })

  it('shows a dedicated loading state only while an existing conversation is restoring', () => {
    expect(source).toContain("conversationId !== 'conv_new' && conversationStore.restoringConversation")
    expect(source).toContain('正在加载对话消息...')
    expect(source).toContain('<n-spin :size="30" />')
    expect(source).toContain('<ChatWorkspace v-else')
  })
})
