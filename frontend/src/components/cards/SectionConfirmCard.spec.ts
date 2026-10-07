import { describe, expect, it } from 'vitest'
import source from './SectionConfirmCard.vue?raw'

describe('SectionConfirmCard floating interaction', () => {
  it('uses the clarification-card shell while preserving section batch controls and the strategy table', () => {
    expect(source).toContain('确认章节处理策略')
    expect(source).toContain('全部 AI 生成')
    expect(source).toContain('恢复建议')
    expect(source).toContain('确认章节策略')
    expect(source).toContain('section-strategy-table')
    expect(source).toMatch(/\.section-card\s*\{[\s\S]*border:\s*1px solid #d9d9d9;/)
    expect(source).toMatch(/\.section-card\s*\{[\s\S]*border-radius:\s*12px;/)
  })

  it('keeps the batch action neutral until the user explicitly applies it and offers only generated-or-template actions', () => {
    expect(source).toContain("const activeBatchAction = ref<SectionAction | null>(null)")
    expect(source).toContain("'section-card__batch-button--active': activeBatchAction === 'ai_generate'")
    expect(source).toContain("activeBatchAction.value = action")
    expect(source).toContain('activeBatchAction.value = null')
    expect(source).toContain("option.value === 'ai_generate' || option.value === 'keep_template'")
    expect(source).not.toContain("{ label: '手动填写', value: 'manual_fill' }")
    expect(source).not.toContain("{ label: '不参与生成', value: 'skip' }")
  })

  it('keeps the batch action neutral until the user explicitly applies it and offers only generated-or-template actions', () => {
    expect(source).toContain("const activeBatchAction = ref<SectionAction | null>(null)")
    expect(source).toContain("'section-card__batch-button--active': activeBatchAction === 'ai_generate'")
    expect(source).toContain("activeBatchAction.value = action")
    expect(source).toContain('activeBatchAction.value = null')
    expect(source).toContain("option.value === 'ai_generate' || option.value === 'keep_template'")
    expect(source).toContain("return action === 'ai_generate' ? action : 'keep_template'")
  })

  it('uses the parsed title as-is instead of prepending the section code again', () => {
    expect(source).toContain('return section.title.trim() || section.code.trim()')
    expect(source).not.toContain("return [section.code, section.title].filter(Boolean).join(' ')")
  })
})
