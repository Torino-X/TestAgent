import type { DynamicNarrative, PublicExecutionUpdate, ToolCall, ToolNarrativeState } from '@/types'

export function dynamicNarrativeTitle(input: Pick<DynamicNarrative, 'agentName' | 'eventType' | 'stepTitle'>): string {
  if (input.agentName === 'PreparationAgent') return 'PreparationAgent 观察/决策'
  return input.stepTitle?.trim()
    || (input.eventType === 'agent_decision_update'
      ? `${input.agentName || 'Agent'} 决策`
      : `${input.agentName || 'Agent'} 观察`)
}

export interface ToolPublicUpdateChoice {
  update: PublicExecutionUpdate
  source: 'llm' | 'deterministic'
  startTime?: string
}

export function chooseToolPublicUpdate(
  toolCall: ToolCall | undefined,
  narratives: ToolNarrativeState[] = [],
): ToolPublicUpdateChoice | undefined {
  if (!toolCall) return undefined

  const narrative = latestMatchingNarrative(toolCall, narratives)
  if (narrative) {
    const llmVisible = narrative.source === 'llm' && (
      narrative.status === 'streaming' || narrative.status === 'completed'
    )
    if (llmVisible) {
      const update = normalizeVisibleUpdate(narrative.publicUpdate)
      if (!update) return undefined
      return {
        update,
        source: 'llm',
        startTime: narrative.createdAt
      }
    }

    if (narrative.status === 'fallback') {
      const update = normalizeVisibleUpdate(narrative.publicUpdate)
      if (update) {
        return {
          update,
          source: 'deterministic',
          startTime: narrative.completedAt ?? narrative.createdAt
        }
      }
    }
  }

  if (toolCall.narrativeExpected && toolCall.status === 'running') return undefined

  if (toolCall.publicUpdate?.headline) {
    return {
      update: toolCall.publicUpdate,
      source: 'deterministic',
      startTime: toolCall.startedAt
    }
  }
  return undefined
}

export function shouldAppendWorkflowThinkingStep(input: {
  isCompleted: boolean
  isFailed: boolean
  hasRunningOrWaitingItem: boolean
}): boolean {
  return !input.isCompleted && !input.isFailed && !input.hasRunningOrWaitingItem
}

function latestMatchingNarrative(
  toolCall: ToolCall,
  narratives: ToolNarrativeState[],
): ToolNarrativeState | undefined {
  const toolCallId = toolCall.id
  if (!toolCallId) return undefined
  const attempt = toolCall.attempt ?? 1
  let matches = narratives.filter(
    (n) => n.sourceToolCallId === toolCallId && (n.attempt ?? 1) === attempt
  )
  if (matches.length === 0 && toolCall.name) {
    matches = narratives.filter(
      (n) => n.toolName === toolCall.name && (n.attempt ?? 1) === attempt
    )
  }
  if (matches.length === 0) return undefined
  return matches.reduce((best, cur) => {
    const rank = (status: ToolNarrativeState['status']) => {
      if (status === 'completed') return 4
      if (status === 'fallback') return 3
      if (status === 'failed') return 2
      return 1
    }
    return rank(cur.status) >= rank(best.status) ? cur : best
  }, matches[0])
}

function normalizeVisibleUpdate(update: PublicExecutionUpdate | undefined): PublicExecutionUpdate | undefined {
  if (!update) return undefined
  const headline = update.headline || update.narrativeText?.slice(0, 80) || ''
  const hasVisibleText = Boolean(
    headline.trim() ||
    update.summary?.trim() ||
    update.impact?.trim() ||
    update.nextAction?.trim() ||
    update.narrativeText?.trim() ||
    update.details?.some((detail) => detail.trim())
  )
  if (!hasVisibleText) return undefined
  return {
    ...update,
    headline
  }
}
