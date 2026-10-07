/**
 * Agent task store — current task state and actions.
 */

import { ref } from 'vue'
import { defineStore } from 'pinia'
import type { AgentTask, SectionItem } from '@/types'
import * as agentApi from '@/api/agentApi'
import type { FormatLossConfirmation } from '@/types'

export const useAgentTaskStore = defineStore('agentTask', () => {
  const currentTask = ref<AgentTask | null>(null)
  const loading = ref(false)
  const pendingFormatLoss = ref<FormatLossConfirmation | null>(null)
  const lastLossDecision = ref<{
    decision: 'accept' | 'retry' | 'timeout'
    nextAction: 'complete' | 're_export'
    fallbackToAccept: boolean
  } | null>(null)

  async function fetchTaskDetail(taskId: string) {
    loading.value = true
    try {
      currentTask.value = await agentApi.fetchTaskDetail(taskId)
    } finally {
      loading.value = false
    }
  }

  async function confirmTask(taskId: string, sections: SectionItem[]) {
    loading.value = true
    try {
      currentTask.value = await agentApi.confirmTask(taskId, sections)
    } finally {
      loading.value = false
    }
  }

  async function cancelTask(taskId: string) {
    loading.value = true
    try {
      currentTask.value = await agentApi.cancelTask(taskId)
    } finally {
      loading.value = false
    }
  }

  async function submitFormatLossDecision(
    taskId: string,
    decision: 'accept' | 'retry',
    note?: string,
  ): Promise<void> {
    loading.value = true
    try {
      const response = await agentApi.submitFormatLossDecision(taskId, decision, note)
      lastLossDecision.value = {
        decision: response.decision,
        nextAction: response.next_action,
        fallbackToAccept: response.fallback_to_accept,
      }
      pendingFormatLoss.value = null
    } finally {
      loading.value = false
    }
  }

  function setPendingFormatLoss(loss: FormatLossConfirmation | null) {
    pendingFormatLoss.value = loss
    if (loss === null) lastLossDecision.value = null
  }

  function getEventsUrl(taskId: string): string {
    return agentApi.createTaskEventSource(taskId).eventsUrl
  }

  function clearTask() {
    currentTask.value = null
    pendingFormatLoss.value = null
    lastLossDecision.value = null
  }

  return {
    currentTask,
    loading,
    pendingFormatLoss,
    lastLossDecision,
    fetchTaskDetail,
    confirmTask,
    cancelTask,
    submitFormatLossDecision,
    setPendingFormatLoss,
    getEventsUrl,
    clearTask
  }
})
