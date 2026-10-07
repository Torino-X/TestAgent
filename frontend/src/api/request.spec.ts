import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError, apiGet, apiPatch, apiPost, clearAuthToken, setAuthToken } from './request'

describe('api request wrapper', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    clearAuthToken()
  })

  it('unwraps unified API responses and sends bearer token', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 0,
          message: 'success',
          data: { user_id: 'user_001' },
          request_id: 'req_001'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)

    setAuthToken('token_abc')

    const data = await apiGet<{ user_id: string }>('/api/auth/me')

    expect(data).toEqual({ user_id: 'user_001' })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/me',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer token_abc'
        })
      })
    )
  })

  it('normalizes common backend error codes into actionable messages', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 40004,
          message: 'file too large',
          data: null,
          request_id: 'req_large_file'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(apiGet('/api/files/too-large')).rejects.toMatchObject({
      message: '文件大小超出限制，请压缩后重新上传。',
      code: 40004,
      status: 200
    } satisfies Partial<ApiRequestError>)
  })

  it('uses endpoint context when the same error code has different business meanings', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            code: 40100,
            message: 'invalid credentials',
            data: null,
            request_id: 'req_login'
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            code: 40900,
            message: 'username exists',
            data: null,
            request_id: 'req_profile'
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            code: 40900,
            message: 'task state conflict',
            data: null,
            request_id: 'req_confirm'
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      )
    vi.stubGlobal('fetch', fetchMock)

    await expect(apiPost('/api/auth/login', { account: 'a', password: 'b' })).rejects.toMatchObject({
      message: '账号或密码错误，请重新输入。',
      requestId: 'req_login'
    } satisfies Partial<ApiRequestError>)
    await expect(apiPatch('/api/auth/me', { username: 'tester' })).rejects.toMatchObject({
      message: '用户名已被占用，请换一个名称。',
      requestId: 'req_profile'
    } satisfies Partial<ApiRequestError>)
    await expect(apiPost('/api/agent/tasks/task_001/confirm', {})).rejects.toMatchObject({
      message: '任务状态已变化，请刷新后查看最新进度。',
      requestId: 'req_confirm'
    } satisfies Partial<ApiRequestError>)
  })

  it('does not translate agent confirmation state errors as file format errors', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 40003,
          message: 'TASK_NOT_WAITING_CONFIRMATION: 任务不在等待确认状态',
          data: null,
          request_id: 'req_confirm_not_ready'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)

    try {
      await apiPost('/api/agent/tasks/task_001/confirm', {})
      throw new Error('Expected request to fail')
    } catch (err) {
      expect(err).toBeInstanceOf(ApiRequestError)
      expect((err as ApiRequestError).message).toContain('任务')
      expect((err as ApiRequestError).message).not.toContain('文件格式')
      expect((err as ApiRequestError).requestId).toBe('req_confirm_not_ready')
    }
  })

  it('normalizes validation, network and non-json backend failures', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: [{ loc: ['body', 'title'], msg: 'field required' }] }), {
          status: 422,
          headers: { 'Content-Type': 'application/json' }
        })
      )
      .mockResolvedValueOnce(new Response('server exploded', { status: 502, headers: { 'Content-Type': 'text/plain' } }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
    vi.stubGlobal('fetch', fetchMock)

    await expect(apiPost('/api/conversations', {})).rejects.toMatchObject({
      message: '请求参数校验失败，请检查填写内容。'
    } satisfies Partial<ApiRequestError>)
    await expect(apiGet('/api/health')).rejects.toMatchObject({
      message: '后端服务暂时不可用，请稍后重试。'
    } satisfies Partial<ApiRequestError>)
    await expect(apiGet('/api/conversations')).rejects.toMatchObject({
      message: '无法连接后端服务，请确认后端已启动并检查网络。'
    } satisfies Partial<ApiRequestError>)
  })

  it('does not fall back to mock data when a backend request fails', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('backend unavailable'))
    vi.stubGlobal('fetch', fetchMock)

    await expect(apiGet('/api/conversations')).rejects.toThrow('backend unavailable')
  })
})
