import { describe, expect, it } from 'vitest'
import source from './ChatMessageList.vue?raw'

describe('ChatMessageList directional spacing', () => {
  it('adds 20% spacing only from an Agent response to the following user message', () => {
    expect(source).toContain('.message-row--assistant + .message-row--user')
    expect(source).toContain('.message-row--agent-run + .message-row--user')
    expect(source).toContain('margin-top: 24px')
    expect(source).toContain('margin-top: 17px')
    expect(source).not.toContain('.message-row--user + .message-row--assistant')
    expect(source).not.toContain('.message-row--user + .message-row--agent-run')
  })
})
