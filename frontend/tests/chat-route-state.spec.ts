import { describe, expect, it } from 'vitest'
import chatViewSource from '../src/views/ChatView.vue?raw'
import conversationStoreSource from '../src/stores/conversationStore.ts?raw'

describe('chat route state isolation', () => {
  it('does not let conversation list changes reset the active chat route state', () => {
    expect(chatViewSource).toContain('watch(')
    expect(chatViewSource).toContain('() => route.params.conversationId')
    expect(chatViewSource).not.toContain('watchEffect')
  })

  it('keeps /chat as a true empty draft instead of falling back to the first history item', () => {
    expect(conversationStoreSource).toContain("if (activeConversationId.value === 'conv_new')")
    expect(conversationStoreSource).toContain('return EMPTY_CONVERSATION')
  })

  it('restores history conversations with their messages and files when opening a route', () => {
    expect(chatViewSource).toContain('conversationStore.restoreConversation(id)')
    expect(conversationStoreSource).toContain("import * as messageApi from '@/api/messageApi'")
    expect(conversationStoreSource).toContain("import * as fileApi from '@/api/fileApi'")
    expect(conversationStoreSource).toContain('async function restoreConversation')
    expect(conversationStoreSource).toContain('messageApi.fetchMessages(id)')
    expect(conversationStoreSource).toContain('fileApi.fetchConversationFiles(id)')
  })

  it('keeps restored details when the sidebar list refreshes later', () => {
    expect(conversationStoreSource).toContain('const summaries = await conversationApi.fetchConversations()')
    expect(conversationStoreSource).toContain('const existing = conversations.value.find')
    expect(conversationStoreSource).toContain('files: existing.files')
    expect(conversationStoreSource).toContain('messages: existing.messages')
  })
})
