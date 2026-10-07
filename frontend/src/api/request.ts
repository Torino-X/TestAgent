const AUTH_TOKEN_KEY = 'testagent.access_token'

export interface RequestOptions {
  headers?: HeadersInit
  signal?: AbortSignal
  onUploadProgress?: (percent: number) => void
}

interface RequestContext {
  method: string
  url: string
}

export interface ApiEnvelope<T> {
  code: number
  message: string
  data: T
  request_id?: string
}

export class ApiRequestError extends Error {
  constructor(
    message: string,
    public readonly code?: number,
    public readonly status?: number,
    public readonly requestId?: string
  ) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

const ERROR_MESSAGES: Record<number, string> = {
  40001: '参数错误，请检查填写内容。',
  40002: '缺少必要参数，请补充后重试。',
  40003: '文件格式不支持，请重新选择文件。',
  40004: '文件大小超出限制，请压缩后重新上传。',
  40100: '登录状态已失效，请重新登录。',
  40101: '登录状态已失效，请重新登录。',
  40300: '当前账号无权执行此操作。',
  40400: '请求的资源不存在或已被删除。',
  40900: '当前资源状态不允许执行该操作。',
  40320: '你没有权限操作该模板。',
  40321: '该模板尚未公开。',
  40322: '你没有权限在该会话中使用模板。',
  40420: '模板不存在或已被删除。',
  40421: '模板版本文件不存在。',
  40920: '已发布模板需要先取消发布才能删除。',
  41320: '模板文件大小超出限制。',
  42220: '模板文件格式不支持，请上传 DOCX 或 XLSX。',
  42221: '模板分类无效，请重新选择。',
  50020: '模板加入会话失败，请稍后重试。',
  50000: '后端服务暂时不可用，请稍后重试。',
  50001: '数据库处理失败，请稍后重试。',
  50002: '文件存储失败，请稍后重试。',
  50102: '文档预览转换器尚未配置，暂时无法在线查看该 Word 文件，请下载文件或联系管理员配置 LibreOffice。',
  51000: '模型调用失败，请检查模型配置。',
  52000: '知识库调用失败，请检查知识库配置。',
  53000: 'Agent 执行失败，请稍后重试。',
  54000: 'Word 导出失败，请稍后重试。'
}

function endpoint(url: string): string {
  return url.split('?')[0]
}

function includesEndpoint(url: string, segment: string): boolean {
  return endpoint(url).includes(segment)
}

function contextMessage(code?: number, status?: number, context?: RequestContext): string | undefined {
  if (!context) return undefined
  const url = endpoint(context.url)
  const method = context.method.toUpperCase()

  if (method === 'POST' && includesEndpoint(url, '/api/auth/login') && (code === 40100 || code === 40101 || status === 401)) {
    return '账号或密码错误，请重新输入。'
  }
  if (method === 'POST' && includesEndpoint(url, '/api/auth/register') && code === 40900) {
    return '邮箱或用户名已被注册，请更换后再试。'
  }
  if (method === 'PATCH' && includesEndpoint(url, '/api/auth/me') && code === 40900) {
    return '用户名已被占用，请换一个名称。'
  }
  if (includesEndpoint(url, '/api/files/upload')) {
    if (code === 40003) return '文件格式不支持，请上传 Word、Markdown、文本或图片等允许的文件。'
    if (code === 40004 || status === 413) return '文件大小超出限制，请压缩后重新上传。'
    if (code === 50002) return '文件保存失败，请稍后重新上传。'
  }
  if (includesEndpoint(url, '/api/templates')) {
    if (code === 42220) return '模板文件格式不支持，请上传有效的 DOCX 或 XLSX 文件。'
    if (code === 41320 || status === 413) return '模板文件大小超出限制，请选择较小的文件。'
    if (code === 40920) return '该模板已发布，请先取消发布后再删除。'
    if (code === 40320 || code === 40321 || code === 40322 || status === 403) return '你没有权限访问或操作该模板。'
    if (code === 40420 || code === 40421 || status === 404) return '模板不存在、已被删除或版本文件缺失。'
    if (code === 50020) return '模板加入会话失败，请稍后重试。'
  }
  if (includesEndpoint(url, '/confirm-type') && (code === 40001 || code === 40002)) {
    return '文件用途确认失败，请重新选择文件类型。'
  }
  if (includesEndpoint(url, '/api/conversations')) {
    if (status === 422) return '请求参数校验失败，请检查填写内容。'
    if (code === 40300) return '你没有权限访问该会话。'
    if (code === 40400) return '会话不存在或已被删除。'
    if (code === 40900) return '当前会话状态不允许执行该操作，请刷新后重试。'
  }
  if (includesEndpoint(url, '/api/agent/tasks')) {
    if (includesEndpoint(url, '/confirm') && code === 40003) return '任务还未进入可确认状态，请稍后再试。'
    if (includesEndpoint(url, '/confirm') && code === 40901) return '任务状态已变化，请刷新后查看最新进度。'
    if (includesEndpoint(url, '/confirm') && code === 40900) return '任务状态已变化，请刷新后查看最新进度。'
    if (includesEndpoint(url, '/cancel') && code === 40900) return '当前任务状态无法取消，请刷新后查看最新进度。'
    if (includesEndpoint(url, '/retry') && code === 40900) return '当前任务状态无法重试，请刷新后查看最新进度。'
    if (code === 40400) return '任务不存在或已被删除。'
    if (code === 53000) return 'Agent 执行失败，请查看对话流中的错误详情。'
  }
  if (includesEndpoint(url, '/api/artifacts')) {
    if (code === 40300) return '你没有权限下载该产物。'
    if (code === 40400) return '产物不存在或已被删除。'
    if (code === 50002) return '产物文件读取失败，请稍后重试。'
    if (code === 54000) return 'Word 产物导出失败，请稍后重试。'
  }
  if (includesEndpoint(url, '/api/projects')) {
    if (status === 422) return '项目参数校验失败，请检查填写内容。'
    if (code === 40300) return '你没有权限访问该项目。'
    if (code === 40400 || code === 40401) return '项目不存在或已被删除。'
    if (code === 40900) return '项目状态已变化，请刷新后重试。'
  }
  if (includesEndpoint(url, '/api/settings/model') && code === 51000) {
    return '模型连接或调用失败，请检查模型地址、密钥和模型名称。'
  }
  if (includesEndpoint(url, '/api/settings/knowledge-base') && code === 52000) {
    return '知识库连接失败，请检查知识库地址、集合名称和密钥。'
  }
  return undefined
}

function normalizeErrorMessage(message: string, code?: number, status?: number, context?: RequestContext): string {
  const contextual = contextMessage(code, status, context)
  if (contextual) return contextual
  if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code]
  if (status === 401) return ERROR_MESSAGES[40100]
  if (status === 403) return ERROR_MESSAGES[40300]
  if (status === 404) return ERROR_MESSAGES[40400]
  if (status === 422) return '请求参数校验失败，请检查填写内容。'
  if (status && status >= 500) return ERROR_MESSAGES[50000]
  return message || '请求失败，请稍后重试。'
}

let memoryToken: string | null = null

function getStorage(): Storage | null {
  return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null
}

export function getAuthToken(): string | null {
  return getStorage()?.getItem(AUTH_TOKEN_KEY) ?? memoryToken
}

export function setAuthToken(token: string | null) {
  memoryToken = token
  const storage = getStorage()
  if (!storage) return
  if (token) {
    storage.setItem(AUTH_TOKEN_KEY, token)
  } else {
    storage.removeItem(AUTH_TOKEN_KEY)
  }
}

export function clearAuthToken() {
  setAuthToken(null)
}

export function resolveApiUrl(url: string): string {
  const base = import.meta.env.VITE_API_BASE_URL as string | undefined
  if (!base || /^https?:\/\//i.test(url)) return url

  const normalizedBase = base.replace(/\/$/, '')
  const normalizedUrl = url.startsWith('/') ? url : `/${url}`
  if (normalizedBase.endsWith('/api') && normalizedUrl.startsWith('/api/')) {
    return `${normalizedBase}${normalizedUrl.slice(4)}`
  }
  return `${normalizedBase}${normalizedUrl}`
}

function buildHeaders(body: unknown, opts?: RequestOptions): HeadersInit {
  const headers: Record<string, string> = {}
  if (!(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
  }

  const token = getAuthToken()
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  return {
    ...headers,
    ...(opts?.headers ?? {})
  }
}

function envelopeCode(payload: unknown): number | undefined {
  if (typeof payload === 'object' && payload !== null && 'code' in payload) {
    const code = Number((payload as { code: unknown }).code)
    return Number.isFinite(code) ? code : undefined
  }
  return undefined
}

function envelopeMessage(payload: unknown, fallback: string): string {
  if (typeof payload === 'object' && payload !== null && 'message' in payload) {
    return String((payload as { message: unknown }).message)
  }
  return fallback
}

function envelopeRequestId(payload: unknown): string | undefined {
  if (typeof payload === 'object' && payload !== null && 'request_id' in payload) {
    return String((payload as { request_id: unknown }).request_id)
  }
  return undefined
}

export async function createApiRequestErrorFromResponse(
  response: Response,
  context?: RequestContext
): Promise<ApiRequestError> {
  const contentType = response.headers.get('Content-Type') ?? ''
  const payload = contentType.includes('application/json') ? await response.json() : await response.text()
  const code = envelopeCode(payload)
  const message = envelopeMessage(payload, `Request failed with status ${response.status}`)
  return new ApiRequestError(
    normalizeErrorMessage(message, code, response.status, context),
    code,
    response.status,
    envelopeRequestId(payload)
  )
}

async function parseResponse<T>(response: Response, context: RequestContext): Promise<T> {
  const contentType = response.headers.get('Content-Type') ?? ''
  const payload = contentType.includes('application/json') ? await response.json() : await response.text()

  if (!response.ok) {
    const code = envelopeCode(payload)
    const message = envelopeMessage(payload, `Request failed with status ${response.status}`)
    throw new ApiRequestError(
      normalizeErrorMessage(message, code, response.status, context),
      code,
      response.status,
      envelopeRequestId(payload)
    )
  }

  if (typeof payload === 'object' && payload !== null && 'code' in payload) {
    const envelope = payload as ApiEnvelope<T>
    if (envelope.code !== 0) {
      throw new ApiRequestError(
        normalizeErrorMessage(envelope.message, envelope.code, response.status, context),
        envelope.code,
        response.status,
        envelope.request_id
      )
    }
    return envelope.data
  }

  return payload as T
}

function parseXhrResponse<T>(xhr: XMLHttpRequest, context: RequestContext): T {
  const contentType = xhr.getResponseHeader('Content-Type') ?? ''
  const responseText = xhr.responseText ?? ''
  let payload: unknown = responseText

  if (contentType.includes('application/json') && responseText) {
    payload = JSON.parse(responseText)
  }

  if (xhr.status < 200 || xhr.status >= 300) {
    const code = envelopeCode(payload)
    const message = envelopeMessage(payload, `Request failed with status ${xhr.status}`)
    throw new ApiRequestError(
      normalizeErrorMessage(message, code, xhr.status, context),
      code,
      xhr.status,
      envelopeRequestId(payload)
    )
  }

  if (typeof payload === 'object' && payload !== null && 'code' in payload) {
    const envelope = payload as ApiEnvelope<T>
    if (envelope.code !== 0) {
      throw new ApiRequestError(
        normalizeErrorMessage(envelope.message, envelope.code, xhr.status, context),
        envelope.code,
        xhr.status,
        envelope.request_id
      )
    }
    return envelope.data
  }

  return payload as T
}

function applyXhrHeaders(xhr: XMLHttpRequest, headers: HeadersInit) {
  new Headers(headers).forEach((value, key) => {
    xhr.setRequestHeader(key, value)
  })
}

function uploadWithProgress<T>(url: string, formData: FormData, opts: RequestOptions): Promise<T> {
  const requestUrl = resolveApiUrl(url)
  const context = { method: 'POST', url }

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    let settled = false

    const cleanup = () => {
      opts.signal?.removeEventListener('abort', handleAbort)
    }
    const settle = (callback: () => void) => {
      if (settled) return
      settled = true
      cleanup()
      callback()
    }
    const handleAbort = () => {
      xhr.abort()
      settle(() => reject(new DOMException('Aborted', 'AbortError')))
    }

    xhr.open('POST', requestUrl)
    xhr.withCredentials = true
    applyXhrHeaders(xhr, buildHeaders(formData, opts))

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable || event.total <= 0) return
      const percent = Math.min(99, Math.max(1, Math.round((event.loaded / event.total) * 100)))
      opts.onUploadProgress?.(percent)
    }

    xhr.onload = () => {
      try {
        const parsed = parseXhrResponse<T>(xhr, context)
        opts.onUploadProgress?.(100)
        settle(() => resolve(parsed))
      } catch (error) {
        settle(() => reject(error))
      }
    }
    xhr.onerror = () => {
      settle(() => reject(new ApiRequestError('无法连接后端服务，请确认后端已启动并检查网络。', undefined, 0)))
    }
    xhr.onabort = () => {
      settle(() => reject(new DOMException('Aborted', 'AbortError')))
    }

    if (opts.signal?.aborted) {
      handleAbort()
      return
    }
    opts.signal?.addEventListener('abort', handleAbort)
    xhr.send(formData)
  })
}

async function request<T>(
  method: string,
  url: string,
  body?: unknown,
  opts?: RequestOptions
): Promise<T> {
  const init: RequestInit = {
    method,
    headers: buildHeaders(body, opts),
    signal: opts?.signal,
    // 让浏览器把 HttpOnly cookie 自动带上（登录响应 Set-Cookie 后，
    // 后续 fetch 自动附带），产物下载的 Blob 流程也靠这一路鉴权
    credentials: 'include'
  }

  if (body !== undefined) {
    init.body = body instanceof FormData ? body : JSON.stringify(body)
  }

  try {
    const response = await fetch(resolveApiUrl(url), init)
    return await parseResponse<T>(response, { method, url })
  } catch (error) {
    if (error instanceof ApiRequestError) throw error
    if (error instanceof TypeError) {
      throw new ApiRequestError('无法连接后端服务，请确认后端已启动并检查网络。', undefined, 0)
    }
    if (error instanceof Error) throw error
    throw new ApiRequestError('Request failed')
  }
}

export async function apiGet<T>(url: string, opts?: RequestOptions): Promise<T> {
  return request<T>('GET', url, undefined, opts)
}

export async function apiPost<T>(url: string, body: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>('POST', url, body, opts)
}

export async function apiPut<T>(url: string, body: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>('PUT', url, body, opts)
}

export async function apiPatch<T>(url: string, body: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>('PATCH', url, body, opts)
}

export async function apiDelete<T>(url: string, opts?: RequestOptions): Promise<T> {
  return request<T>('DELETE', url, undefined, opts)
}

export async function apiUpload<T>(url: string, formData: FormData, opts?: RequestOptions): Promise<T> {
  if (opts?.onUploadProgress) return uploadWithProgress<T>(url, formData, opts)
  return request<T>('POST', url, formData, opts)
}

function sanitizeDownloadFilename(value: string): string | undefined {
  const filename = value
    .replace(/^['"]|['"]$/g, '')
    .replace(/[\\/]/g, '_')
    .replace(/[\u0000-\u001f\u007f]/g, '')
    .trim()
  return filename || undefined
}

function parseContentDispositionFilename(value: string): string | undefined {
  const utf8Match = value.match(/(?:^|;)\s*filename\*\s*=\s*(?:UTF-8'')?([^;\r\n]+)/i)
  if (utf8Match?.[1]) {
    const encodedFilename = utf8Match[1].trim()
    try {
      return sanitizeDownloadFilename(decodeURIComponent(encodedFilename))
    } catch {
      return sanitizeDownloadFilename(encodedFilename)
    }
  }

  const simpleMatch = value.match(/(?:^|;)\s*filename\s*=\s*(?:"([^"]*)"|([^;\r\n]*))/i)
  return sanitizeDownloadFilename(simpleMatch?.[1] ?? simpleMatch?.[2] ?? '')
}

/**
 * Authenticated Blob download using the same request headers and credentials
 * as the regular API client. The caller owns the browser download UX.
 */
export async function apiDownloadBlob(
  url: string,
  opts?: RequestOptions
): Promise<{ blob: Blob; filename?: string; contentType: string }> {
  const requestUrl = resolveApiUrl(url)
  try {
    const response = await fetch(requestUrl, {
      method: 'GET',
      headers: buildHeaders(undefined, opts),
      credentials: 'include'
    })

    if (!response.ok) {
      throw await createApiRequestErrorFromResponse(response, {
        method: 'GET',
        url: requestUrl
      })
    }

    const contentType = response.headers.get('Content-Type') ?? ''
    const contentDisposition = response.headers.get('Content-Disposition') ?? ''
    const blob = await response.blob()
    return {
      blob,
      filename: parseContentDispositionFilename(contentDisposition),
      contentType
    }
  } catch (error) {
    if (error instanceof ApiRequestError) throw error
    if (error instanceof TypeError) {
      throw new ApiRequestError('无法连接后端服务，请确认后端已启动并检查网络。', undefined, 0)
    }
    if (error instanceof Error) throw error
    throw new ApiRequestError('请求失败，请稍后重试。')
  }
}
