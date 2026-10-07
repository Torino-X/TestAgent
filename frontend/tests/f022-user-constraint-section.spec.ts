import { describe, expect, it } from 'vitest'
import agentRunCardSource from '../src/components/cards/AgentRunCard.vue?raw'
import typesSource from '../src/types/index.ts?raw'
import eventPayloadSource from '../src/utils/eventPayload.ts?raw'
import { normalizeConfirmationSectionsItems } from '../src/utils/eventPayload'

describe('F022 frontend — user-constraint section markers', () => {
  it('declares SectionConstraintSource in the shared types module', () => {
    expect(typesSource).toContain('SectionConstraintSource')
    expect(typesSource).toContain("'user_prompt'")
    expect(typesSource).toContain("'template'")
  })

  it('adds an optional constraintSource field on SectionItem', () => {
    expect(typesSource).toMatch(
      /interface\s+SectionItem[\s\S]*?constraintSource\?:\s*SectionConstraintSource/,
    )
  })

  it('maps backend constraint_source into frontend constraintSource', () => {
    // Phase 2.9A.35: 该映射现在位于统一确认契约层 eventPayload.ts
    // (normalizeConfirmationSectionsItems),useTaskEvents 通过它消费。
    // 单一 SSOT,不再在 useTaskEvents 内维护三套字段读取。
    expect(eventPayloadSource).toContain('constraint_source')
    expect(eventPayloadSource).toContain('constraintSource')
    // Only the canonical "user_prompt" string survives sanitisation;
    // anything else is dropped to undefined so the badge stays hidden.
    expect(eventPayloadSource).toMatch(
      /constraint_source.*constraintSource.*===\s*['"]user_prompt['"]/s,
    )
    // 行为验证: user_prompt 保留,其它值 → undefined
    const items = normalizeConfirmationSectionsItems({
      sections: [
        { section_id: 's1', section_title: 'S1', constraint_source: 'user_prompt' },
        { section_id: 's2', section_title: 'S2', constraint_source: 'template' }
      ]
    })
    expect(items[0].constraintSource).toBe('user_prompt')
    expect(items[1].constraintSource).toBeUndefined()
  })

  it('renders a "用户指定" chip next to overridden section titles', () => {
    expect(agentRunCardSource).toContain('section-user-constraint-chip')
    expect(agentRunCardSource).toContain('用户指定')
    // The chip must be conditionally rendered on constraintSource ===
    // "user_prompt" — not unconditionally emitted.
    expect(agentRunCardSource).toMatch(
      /section\.constraintSource\s*===\s*['"]user_prompt['"]/,
    )
  })

  it('tags the row with a distinct background when the section is user-constrained', () => {
    expect(agentRunCardSource).toContain('section-row--user-constrained')
  })

  it('surfaces a header hint when at least one section is overridden', () => {
    expect(agentRunCardSource).toContain('userConstraintCount')
    expect(agentRunCardSource).toContain('section-strategy-user-hint')
    expect(agentRunCardSource).toContain('其中 <strong>{{ userConstraintCount }}</strong> 个按你的提示词指定')
  })

  it('does not regress existing section-strategy structure', () => {
    expect(agentRunCardSource).toContain('section-strategy-table')
    expect(agentRunCardSource).toContain('section-suggestion-chip')
    expect(agentRunCardSource).toContain('<AppSelect')
    expect(agentRunCardSource).toContain('restoreSuggestedActions')
  })
})
