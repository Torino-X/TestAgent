import { describe, expect, it } from 'vitest'
import { formatDisplayTime, parseServerTime } from '@/utils/time'

describe('time utility', () => {
  it('parses ISO with explicit Z', () => {
    const d = parseServerTime('2026-07-29T08:11:23.456789Z')
    expect(d.getUTCFullYear()).toBe(2026)
    expect(d.toISOString()).toBe('2026-07-29T08:11:23.456Z')
  })

  it('parses naive ISO as UTC', () => {
    const d = parseServerTime('2026-07-29T08:11:23')
    expect(d.toISOString()).toBe('2026-07-29T08:11:23.000Z')
  })

  it('preserves explicit offset', () => {
    const d = parseServerTime('2026-07-29T08:11:23+08:00')
    expect(d.getUTCHours()).toBe(0) // 8 - 8 = 0 UTC
  })

  it('handles null/undefined without throwing', () => {
    expect(Number.isNaN(parseServerTime(null).getTime())).toBe(true)
    expect(Number.isNaN(parseServerTime(undefined).getTime())).toBe(true)
    expect(Number.isNaN(parseServerTime('').getTime())).toBe(true)
  })

  it('formatDisplayTime renders hour:minute', () => {
    const out = formatDisplayTime('2026-07-29T08:11:23Z')
    // Locale formatting varies by environment; check non-empty.
    expect(out).toMatch(/^\d{2}:\d{2}$/)
  })

  it('formatDisplayTime returns empty on bad input', () => {
    expect(formatDisplayTime('')).toBe('')
    expect(formatDisplayTime(null)).toBe('')
  })
})