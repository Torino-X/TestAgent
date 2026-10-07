<template>
  <AppLayout>
    <section
      v-if="isLoadingConversation"
      class="conversation-loading-state"
      role="status"
      aria-live="polite"
    >
      <n-spin :size="30" />
      <p>正在加载对话消息...</p>
    </section>
    <ChatWorkspace v-else :conversation="conversationStore.activeConversation" />
  </AppLayout>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { NSpin } from 'naive-ui'
import AppLayout from '@/components/layout/AppLayout.vue'
import ChatWorkspace from '@/components/chat/ChatWorkspace.vue'
import { useConversationStore } from '@/stores/conversationStore'

const route = useRoute()
const conversationStore = useConversationStore()
const isLoadingConversation = computed(() => {
  const conversationId = route.params.conversationId
  return typeof conversationId === 'string' && conversationId !== 'conv_new' && conversationStore.restoringConversation
})

watch(
  () => route.params.conversationId,
  async (conversationId) => {
    const id = typeof conversationId === 'string' ? conversationId : undefined
    if (!id || id === 'conv_new') {
      conversationStore.setActiveConversation()
      return
    }
    conversationStore.setActiveConversation(id)
    await conversationStore.restoreConversation(id)
  },
  { immediate: true }
)
</script>

<style scoped>
.conversation-loading-state {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
  min-width: 0;
  padding-bottom: 72px;
  color: var(--ta-text-muted);
  background: var(--ta-background);
}

.conversation-loading-state p {
  margin: 0;
  font-size: 14px;
  line-height: 22px;
}
</style>
