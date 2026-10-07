import { describe, expect, it } from 'vitest'
import type { PublicExecutionUpdate, ToolCall, ToolNarrativeState } from '@/types'
import {
  chooseToolPublicUpdate,
  dynamicNarrativeTitle,
  shouldAppendWorkflowThinkingStep
} from './agentRunTimeline'

function publicUpdate(headline: string): PublicExecutionUpdate {
  return {
    version: 1,
    kind: 'tool_result',
    level: 'success',
    headline,
    summary: headline,
    impact: '',
    nextAction: '',
    details: [],
    source: 'template',
    dedupeKey: headline
  }
}

function toolCall(overrides: Partial<ToolCall> = {}): ToolCall {
  return {
    id: 'TemplateParserTool-call',
    name: 'TemplateParserTool',
    status: 'success',
    input: '',
    output: '',
    duration: '',
    publicUpdate: publicUpdate('deterministic template narrative'),
    ...overrides
  }
}

function narrative(overrides: Partial<ToolNarrativeState> = {}): ToolNarrativeState {
  return {
    narrativeId: 'nar-1',
    generationId: 'gen-1',
    generationNo: 1,
    sourceToolCallId: 'TemplateParserTool-call',
    toolName: 'TemplateParserTool',
    attempt: 1,
    status: 'streaming',
    source: 'llm',
    publicUpdate: publicUpdate(''),
    lastChunkIndex: -1,
    createdAt: '2026-08-08T12:00:00+00:00',
    ...overrides
  }
}

describe('agent run timeline helpers', () => {
  it('does not show deterministic tool text while a matching LLM narrative is streaming but empty', () => {
    const choice = chooseToolPublicUpdate(toolCall(), [
      narrative({ publicUpdate: publicUpdate('') })
    ])

    expect(choice).toBeUndefined()
  })

  it('does not show deterministic tool text while an expected narrative has not arrived yet', () => {
    const choice = chooseToolPublicUpdate(toolCall({ status: 'running', narrativeExpected: true }), [])

    expect(choice).toBeUndefined()
  })

  it('falls back to deterministic tool text after completion when expected narrative never arrives', () => {
    const choice = chooseToolPublicUpdate(toolCall({ status: 'success', narrativeExpected: true }), [])

    expect(choice?.source).toBe('deterministic')
    expect(choice?.update.headline).toBe('deterministic template narrative')
  })

  it('shows deterministic tool text when the matching LLM narrative failed', () => {
    const choice = chooseToolPublicUpdate(toolCall(), [
      narrative({ status: 'failed' })
    ])

    expect(choice?.source).toBe('deterministic')
    expect(choice?.update.headline).toBe('deterministic template narrative')
  })

  it('shows LLM tool text once the matching narrative has content', () => {
    const choice = chooseToolPublicUpdate(toolCall(), [
      narrative({
        publicUpdate: {
          ...publicUpdate('LLM generated narrative'),
          narrativeText: 'LLM generated narrative'
        }
      })
    ])

    expect(choice?.source).toBe('llm')
    expect(choice?.update.headline).toBe('LLM generated narrative')
  })

  it('falls back to tool name when narrative source id does not match the tool card id', () => {
    const choice = chooseToolPublicUpdate(toolCall({
      id: 'tool_task_RequirementParserTool',
      name: 'RequirementParserTool',
      narrativeExpected: true
    }), [
      narrative({
        sourceToolCallId: 'RequirementParserTool-call-from-backend',
        toolName: 'RequirementParserTool',
        publicUpdate: {
          ...publicUpdate('LLM Word parser narrative'),
          narrativeText: 'LLM Word parser narrative'
        }
      })
    ])

    expect(choice?.source).toBe('llm')
    expect(choice?.update.headline).toBe('LLM Word parser narrative')
  })

  it('keeps a running thinking step after non-terminal failed tools instead of revealing review prematurely', () => {
    expect(shouldAppendWorkflowThinkingStep({
      isCompleted: false,
      isFailed: false,
      hasRunningOrWaitingItem: false
    })).toBe(true)
  })

  it('does not add the workflow thinking step once another item is already active', () => {
    expect(shouldAppendWorkflowThinkingStep({
      isCompleted: false,
      isFailed: false,
      hasRunningOrWaitingItem: true
    })).toBe(false)
  })

  it('gives PreparationAgent one combined observation and decision title', () => {
    expect(dynamicNarrativeTitle({
      agentName: 'PreparationAgent',
      eventType: 'agent_decision_update',
    })).toBe('PreparationAgent 观察/决策')
  })
})
