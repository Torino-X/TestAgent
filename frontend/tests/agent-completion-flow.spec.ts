import { describe, expect, it } from 'vitest'
import artifactCardSource from '../src/components/cards/ArtifactDownloadCard.vue?raw'
import chatMessageListSource from '../src/components/chat/ChatMessageList.vue?raw'
import chatWorkspaceSource from '../src/components/chat/ChatWorkspace.vue?raw'
import conversationStoreSource from '../src/stores/conversationStore.ts?raw'
import errorCardSource from '../src/components/cards/ErrorMessageCard.vue?raw'

describe('agent completion and recovery flow', () => {
  it('wires artifact download from the card to the workspace API action', () => {
    expect(artifactCardSource).toContain("download: [artifactId: string, fallbackName?: string]")
    expect(chatMessageListSource).toContain('@download=')
    expect(chatMessageListSource).toContain("'download-artifact'")
    expect(chatWorkspaceSource).toContain('async function handleDownloadArtifact')
    expect(chatWorkspaceSource).toContain('artifactApi.downloadArtifact')
  })

  it('exposes task cancel and failed-task retry controls without changing global navigation', () => {
    expect(chatWorkspaceSource).toContain('handleRetryTask')
    expect(chatWorkspaceSource).toContain('agentApi.retryTask')
    expect(errorCardSource).toContain("retry: []")
    expect(chatMessageListSource).toContain("'retry-task'")
  })

  it('restores task event-list, pending confirmation and completed artifacts for history sessions', () => {
    // Phase 2.9A.30: the store now uses reduceTaskEvents from useTaskEventReducer
    // and restoreAllTaskRuns to restore all tasks with detail+events+artifacts.
    expect(conversationStoreSource).toContain("import * as agentApi from '@/api/agentApi'")
    expect(conversationStoreSource).toContain("import * as artifactApi from '@/api/artifactApi'")
    expect(conversationStoreSource).toContain('reduceTaskEvents')
    expect(conversationStoreSource).toContain('agentApi.fetchAllTaskEvents')
    expect(conversationStoreSource).toContain('artifactApi.fetchTaskArtifacts')
    expect(conversationStoreSource).toContain('taskRunBlocks')
  })
})
