import { describe, expect, it } from 'vitest'
import routerSource from '../src/router/index.ts?raw'
import authStoreSource from '../src/stores/authStore.ts?raw'
import requestSource from '../src/api/request.ts?raw'

describe('auth restore contract', () => {
  it('restores the current user before allowing protected routes', () => {
    expect(authStoreSource).toContain('async function restoreSession')
    expect(routerSource).toContain('await authStore.restoreSession()')
    expect(routerSource).toContain('next({ path: \'/login\'')
  })

  it('does not expose a local fallback switch in the request layer', () => {
    const legacyUseFlag = ['VITE_USE', ['MO', 'CK_API'].join('')].join('_')
    const legacyDisableFlag = ['VITE_DISABLE', ['MO', 'CK_FALLBACK'].join('')].join('_')

    expect(requestSource).not.toContain('fallbackData')
    expect(requestSource).not.toContain('mockData')
    expect(requestSource).not.toContain(legacyUseFlag)
    expect(requestSource).not.toContain(legacyDisableFlag)
  })
})
