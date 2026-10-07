import { describe, expect, it } from 'vitest'
import source from './PreparationClarificationCard.vue?raw'

describe('PreparationClarificationCard guided flow', () => {
  it('shows one clarification at a time with an Other input and progressive actions', () => {
    expect(source).toContain('{{ currentIndex + 1 }}/{{ cards.length }}')
    expect(source).toContain('v-for="option in activeOptions"')
    expect(source).toContain('其他')
    expect(source).toContain('placeholder="请输入你的补充说明"')
    expect(source).toContain('Back')
    expect(source).toContain('Skip')
    expect(source).toContain("isLastCard ? '提交' : 'Next'")
  })

  it('serializes skipped questions as an explicit task-scoped answer and keeps conservative scope opt-in', () => {
    expect(source).toContain("answers[card.id] = '【已跳过】用户暂未确认此项。'")
    expect(source).toContain('conservativeGapIds.push(card.id)')
    expect(source).toContain("const conservativeOptionId = '__conservative_scope__'")
  })

  it('keeps the long question header and the controls on one aligned row with a green progress badge', () => {
    expect(source).toMatch(/\.clarification-card__heading\s*\{[\s\S]*flex:\s*1;/)
    expect(source).toMatch(/\.clarification-card__header-actions\s*\{[\s\S]*flex:\s*0 0 auto;/)
    expect(source).toMatch(/\.clarification-card__progress\s*\{[\s\S]*background:\s*#dcfce7;/)
    expect(source).toContain('ChevronDownOutline')
    expect(source).toContain('CloseOutline')
    expect(source).toContain("'layout-change': [expanded: boolean]")
    expect(source).toContain("emit('layout-change', !nextCollapsed)")
    expect(source).toMatch(/\.clarification-card__button--primary\s*\{[\s\S]*background:\s*#0d0d0d;/)
  })
})
