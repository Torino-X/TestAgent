import { describe, expect, it } from 'vitest'
import source from './FormatLossConfirmCard.vue?raw'

describe('FormatLossConfirmCard', () => {
  it('uses the same confirmation-card surface and action layout as the floating user-confirmation cards', () => {
    expect(source).toContain('width: min(100%, 780px)')
    expect(source).toContain('border-radius: 12px')
    expect(source).toContain('class="format-loss-card__footer"')
    expect(source).toContain('format-loss-card__button--accept')
    expect(source).toContain('format-loss-card__button--retry')
  })

  it('shows the detected losses and submits the existing accept or retry decision', () => {
    expect(source).toContain('v-for="(loss, index) in confirmation.losses"')
    expect(source).toContain("emit('decision', choice.id)")
    expect(source).toContain("{ id: 'retry', label: '重新生成文档' }")
    expect(source).toContain("{ id: 'accept', label: '接受格式丢失并继续' }")
  })
})
