import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import appSource from '../src/App.vue?raw'

const mainCssSource = readFileSync(new URL('../src/styles/main.css', import.meta.url), 'utf8')
const tokensSource = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8')

function tokenValue(name: string) {
  const line = tokensSource.split(/\r?\n/).find((item: string) => item.trim().startsWith(`${name}:`))
  return line?.split(':').slice(1).join(':').replace(';', '').trim()
}

describe('main page neutral tone contract', () => {
  it('uses the Stitch black and white design system for global theme tokens', () => {
    expect(tokenValue('--ta-background')).toBe('#f9f9f9')
    expect(tokenValue('--ta-bg-main')).toBe('#ffffff')
    expect(tokenValue('--ta-surface')).toBe('#ffffff')
    expect(tokenValue('--ta-surface-low')).toBe('#f3f4f6')
    expect(tokenValue('--ta-surface-high')).toBe('#e5e7eb')
    expect(tokenValue('--ta-primary')).toBe('#000000')
    expect(tokenValue('--ta-primary-container')).toBe('#1b1b1b')
    expect(tokenValue('--ta-primary-fixed')).toBe('#e2e2e2')
    expect(tokenValue('--ta-secondary')).toBe('#585f6c')
  })

  it('keeps Naive UI primary controls on the same neutral palette', () => {
    expect(appSource).toContain("primaryColor: '#000000'")
    expect(appSource).toContain("primaryColorHover: '#1f2937'")
    expect(appSource).toContain("primaryColorPressed: '#111111'")
    expect(appSource).not.toContain('#004ac6')
    expect(appSource).not.toContain('#2563eb')
  })

  it('removes dominant blue focus and pulse accents from shared styles', () => {
    expect(mainCssSource).not.toContain('rgba(0, 74, 198')
    expect(mainCssSource).not.toContain('rgba(59, 130, 246')
  })
})
