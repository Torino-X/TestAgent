import { describe, expect, it } from 'vitest'
import source from './ChatWorkspace.vue?raw'

describe('ChatWorkspace format-loss confirmation', () => {
  it('renders format-loss confirmation beside the existing floating confirmation cards', () => {
    expect(source).toContain('FormatLossConfirmCard')
    expect(source).toContain('class="chat-workspace__format-loss-confirmation"')
    expect(source).toContain(':format-loss-external="true"')
    expect(source).toContain('pendingFormatLossConfirmation')
  })

  it('keeps enough scroll space and reports a failed decision inline', () => {
    expect(source).toContain('chat-scroll--floating-confirmation')
    expect(source).toContain('formatLossDecisionError')
    expect(source).toContain('handlePendingFormatLossDecision')
  })

  it('replaces the pending card with a local receipt without restoring the full conversation', () => {
    const start = source.indexOf('async function handleFormatLossDecision')
    const end = source.indexOf('\nfunction handleToast', start)
    const handler = source.slice(start, end)

    expect(handler).toContain('confirmationReceipt: buildFormatLossConfirmationReceipt')
    expect(handler).toContain('connectTaskEvents(props.conversation.id, task.events_url, task.task_id)')
    expect(handler).not.toContain('restoreConversation')
  })
})
