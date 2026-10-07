import { describe, expect, it } from 'vitest'
import source from '../src/components/cards/AgentRunCard.vue?raw'

describe('AgentRunCard task process presentation', () => {
  it('uses a standalone duration header and a separately collapsible process area', () => {
    expect(source).toContain('task-duration-header')
    expect(source).toContain('task-process')
    expect(source).toContain('canToggleProcess')
    expect(source).toContain('aria-expanded')
    expect(source).toContain('aria-controls="task-process"')
  })

  it('keeps active, failed, and waiting task processes visible while only successful completion collapses by default', () => {
    // Phase 2.9A.30: uses isTerminalState instead of isSuccessfulTerminalState
    expect(source).toContain('isTerminalState')
    expect(source).toContain('const shouldShowProcess')
    expect(source).toContain('processExpanded')
  })

  it('does not restore an actionable section confirmation card after the task has completed', () => {
    expect(source).toContain('const hasTerminalCompletion')
    expect(source).toContain('const rawSectionMessage')
    expect(source).toContain('if (hasTerminalCompletion.value) return undefined')
  })

  it('renders final summaries and artifact cards outside the collapsible process', () => {
    expect(source).toMatch(/<Transition name="task-process"[\s\S]*?<\/Transition>[\s\S]*?task-completion-summary[\s\S]*?artifact-result-grid/)
  })

  it('shows a live elapsed duration before completion and preserves the completed duration toggle', () => {
    expect(source).toContain('task-duration-live')
    expect(source).toContain('const nowMs = ref(Date.now())')
    expect(source).toContain('startTaskDurationTicker')
    expect(source).toContain('hasTaskDuration')
  })

  it('calculates tool durations instead of leaving completed tool calls in a pending duration state', () => {
    expect(source).toContain('function formatToolDuration')
    expect(source).toContain('duration: formatToolDuration(message)')
    expect(source).not.toContain("message.toolCall?.duration || '统计中'")
  })
})
