import { describe, expect, it } from 'vitest'
import { routes } from './routes'

describe('router context productization', () => {
  it('removes the ordinary user /context management route', () => {
    expect(routes.some((route) => route.path === '/context')).toBe(false)
  })
})

describe('library route', () => {
  it('exposes the standalone library page', () => {
    expect(routes.some((route) => route.path === '/library' && route.name === 'library')).toBe(true)
  })

  it('exposes an immersive document preview route', () => {
    expect(routes.some((route) => route.path === '/library/items/:itemId/preview' && route.name === 'document-preview')).toBe(true)
  })
})

describe('template marketplace routes', () => {
  it('exposes the market and shared read-only preview routes', () => {
    expect(routes.some((route) => route.path === '/templates' && route.name === 'templates')).toBe(true)
    expect(routes.some((route) => route.path === '/templates/:templateId/preview' && route.name === 'template-preview')).toBe(true)
    expect(routes.some((route) => route.path === '/templates/mine/:userTemplateId/preview' && route.name === 'my-template-preview')).toBe(true)
  })
})
