import { describe, expect, it } from 'vitest'
import baseListSource from '../src/components/knowledge/KnowledgeBaseList.vue?raw'
import labelManagerSource from '../src/components/knowledge/KnowledgeLabelManager.vue?raw'

function buttonStyle(source: string) {
  const match = source.match(/\.button\s*\{(?<body>[\s\S]*?)\n\}/)
  expect(match?.groups?.body).toBeTruthy()
  return match?.groups?.body ?? ''
}

describe('knowledge action buttons', () => {
  it('keeps primary toolbar button labels on one line', () => {
    expect(buttonStyle(baseListSource)).toContain('white-space: nowrap')
    expect(buttonStyle(baseListSource)).toContain('flex: 0 0 auto')
    expect(buttonStyle(labelManagerSource)).toContain('white-space: nowrap')
    expect(buttonStyle(labelManagerSource)).toContain('flex: 0 0 auto')
  })
})
