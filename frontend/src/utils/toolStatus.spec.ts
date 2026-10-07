import { describe, expect, it } from 'vitest'
import { resolveToolDisplayStatus, toolDisplayStatusLabel } from './toolStatus'

describe('tool status resolver', () => {
  it('keeps running as running', () => {
    expect(resolveToolDisplayStatus('running')).toBe('running')
  })

  it('surfaces retrying from the public update without changing tool status', () => {
    expect(resolveToolDisplayStatus('running', { level: 'retrying' })).toBe('retrying')
  })

  it('maps warning and failure to user-facing labels', () => {
    expect(resolveToolDisplayStatus('success', { level: 'warning' })).toBe('warning')
    expect(toolDisplayStatusLabel('failed')).toBe('执行失败')
  })
})
