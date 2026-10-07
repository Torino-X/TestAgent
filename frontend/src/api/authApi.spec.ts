import { afterEach, describe, expect, it, vi } from 'vitest'
import { login, register, updateCurrentUser } from './authApi'
import { clearAuthToken } from './request'

describe('authApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    clearAuthToken()
  })

  it('registers using the documented register payload and maps the user profile', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 0,
          message: 'success',
          data: {
            access_token: 'token_register',
            token_type: 'bearer',
            expires_in: 86400,
            user: {
              user_id: 'user_001',
              username: 'name',
              display_name: 'name',
              email: 'name@company.com',
              role: 'user',
              status: 'active',
              avatar_url: null
            }
          },
          request_id: 'req_register'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await register({
      email: 'name@company.com',
      password: 'Password123',
      confirmPassword: 'Password123',
      agreeTerms: true
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/register',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          email: 'name@company.com',
          password: 'Password123',
          confirm_password: 'Password123',
          agree_terms: true
        })
      })
    )
    expect(result).toEqual({
      token: 'token_register',
      user: expect.objectContaining({
        id: 'user_001',
        name: 'name',
        username: 'name',
        email: 'name@company.com'
      })
    })
  })

  it('logs in with username and stores returned token', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 0,
          message: 'success',
          data: {
            access_token: 'token_login',
            user: {
              user_id: 'user_admin',
              username: 'admin',
              display_name: '管理员',
              email: 'admin@example.com',
              role: 'admin',
              status: 'active',
              avatar_url: null
            }
          },
          request_id: 'req_login'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await login({ account: 'admin', password: '123456' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/login',
      expect.objectContaining({
        body: JSON.stringify({ username: 'admin', password: '123456' })
      })
    )
    expect(result.token).toBe('token_login')
  })

  it('maps the backend login response shape with token and profile id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 0,
          message: 'success',
          data: {
            token: 'token_from_login',
            user: {
              user_id: 'user_46708216',
              username: 'xiaoliux',
              display_name: 'xiaoliux',
              email: 'xiaoliux@qq.com',
              role: 'user',
              status: 'active',
              avatar_url: null
            }
          },
          request_id: 'req_login'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await login({ account: 'xiaoliux@qq.com', password: '123456' })

    expect(result).toEqual({
      token: 'token_from_login',
      user: expect.objectContaining({
        id: 'user_46708216',
        name: 'xiaoliux',
        username: 'xiaoliux',
        email: 'xiaoliux@qq.com'
      })
    })
  })

  it('updates current user through PATCH /api/auth/me', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 0,
          message: 'success',
          data: {
            user_id: 'user_001',
            username: 'xiaoyunyunx',
            display_name: 'XiaoYun',
            email: 'xiaoyun@example.com',
            role: 'user',
            status: 'active',
            avatar_url: null
          },
          request_id: 'req_profile'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)

    const user = await updateCurrentUser({
      displayName: 'XiaoYun',
      username: 'xiaoyunyunx',
      avatarUrl: null
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/me',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({
          display_name: 'XiaoYun',
          username: 'xiaoyunyunx',
          avatar_url: null
        })
      })
    )
    expect(user.name).toBe('XiaoYun')
  })
})
