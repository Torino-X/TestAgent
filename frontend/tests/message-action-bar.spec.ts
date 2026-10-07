import { describe, expect, it } from 'vitest'
import chatMessageListSource from '../src/components/chat/ChatMessageList.vue?raw'

describe('assistant message action bar', () => {
  it('uses the Stitch-designed four-action hover treatment', () => {
    expect(chatMessageListSource).toContain('data-tooltip="复制回复"')
    expect(chatMessageListSource).toContain('data-tooltip="喜欢"')
    expect(chatMessageListSource).toContain('data-tooltip="不喜欢"')
    expect(chatMessageListSource).toContain('data-tooltip="重新生成"')
    expect(chatMessageListSource).toContain('width: 32px')
    expect(chatMessageListSource).toContain('gap: 4px')
    expect(chatMessageListSource).toContain('background: #f7f7f8')
    expect(chatMessageListSource).toContain('.message-action-button::after')
  })
})
