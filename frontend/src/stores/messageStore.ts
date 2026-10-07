/**
 * Message store — messages for the current conversation.
 */

import { ref } from 'vue'
import { defineStore } from 'pinia'
import type { ChatMessage, SendMessageResponse } from '@/types'
import * as messageApi from '@/api/messageApi'

export const useMessageStore = defineStore('message', () => {
  const messages = ref<ChatMessage[]>([])
  const sending = ref(false)

  async function fetchMessages(conversationId: string) {
    messages.value = await messageApi.fetchMessages(conversationId)
  }

  async function sendMessage(
    conversationId: string,
    text: string,
    fileIds?: string[]
  ): Promise<SendMessageResponse> {
    sending.value = true
    try {
      const res = await messageApi.sendMessage(conversationId, text, fileIds)
      // Append user message and agent reply to local state
      messages.value = [...messages.value, res.message]
      if (res.agent_reply) {
        messages.value = [...messages.value, res.agent_reply]
      }
      return res
    } finally {
      sending.value = false
    }
  }

  /** Append a message (used by SSE event handler) */
  function appendMessage(message: ChatMessage) {
    messages.value = [...messages.value, message]
  }

  /** Update an existing message in-place (used by SSE to update tool_call etc.) */
  function updateMessage(messageId: string, patch: Partial<ChatMessage>) {
    const idx = messages.value.findIndex((m) => m.id === messageId)
    if (idx >= 0) {
      messages.value[idx] = { ...messages.value[idx], ...patch }
      return true
    }
    return false
  }

  function clearMessages() {
    messages.value = []
  }

  return {
    messages,
    sending,
    fetchMessages,
    sendMessage,
    appendMessage,
    updateMessage,
    clearMessages
  }
})
