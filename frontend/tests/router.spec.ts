import { describe, expect, it } from 'vitest'
import { routes } from '@/router/routes'

describe('router contract', () => {
  it('exposes the first-round TestAgent routes', () => {
    expect(routes.map((route) => route.path)).toEqual(
      expect.arrayContaining(['/login', '/register', '/chat', '/chat/:conversationId', '/settings'])
    )
  })
})
