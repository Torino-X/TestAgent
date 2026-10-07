import type { LoginRequest, LoginResponse, UserProfile } from '@/types'
import { apiGet, apiPatch, apiPost, clearAuthToken, setAuthToken } from './request'

interface ApiUser {
  user_id?: string
  id?: string
  username: string
  display_name?: string
  name?: string
  email?: string
  role: string
  status?: string
  avatar_url?: string | null
}

interface AuthTokenResponse {
  access_token?: string
  token?: string
  token_type?: string
  expires_in?: number
  user: ApiUser
}

export interface RegisterRequest {
  email: string
  password: string
  confirmPassword: string
  agreeTerms: boolean
}

export interface UpdateCurrentUserRequest {
  displayName?: string
  username?: string
  avatarUrl?: string | null
}

function mapUser(user: ApiUser): UserProfile {
  return {
    id: user.user_id ?? user.id ?? '',
    name: user.display_name || user.name || user.username,
    username: user.username,
    email: user.email,
    role: user.role,
    status: user.status,
    avatarUrl: user.avatar_url ?? null
  }
}

function mapAuthResponse(data: AuthTokenResponse): LoginResponse {
  const token = data.access_token ?? data.token ?? ''
  setAuthToken(token)
  return {
    token,
    user: mapUser(data.user)
  }
}

export async function register(req: RegisterRequest): Promise<LoginResponse> {
  const data = await apiPost<AuthTokenResponse>(
    '/api/auth/register',
    {
      email: req.email,
      password: req.password,
      confirm_password: req.confirmPassword,
      agree_terms: req.agreeTerms
    }
  )
  return mapAuthResponse(data)
}

export async function login(req: LoginRequest): Promise<LoginResponse> {
  const data = await apiPost<AuthTokenResponse>(
    '/api/auth/login',
    {
      username: req.account,
      password: req.password
    }
  )
  return mapAuthResponse(data)
}

export async function logout(): Promise<void> {
  try {
    await apiPost('/api/auth/logout', {})
  } finally {
    clearAuthToken()
  }
}

export async function fetchCurrentUser(): Promise<UserProfile> {
  const user = await apiGet<ApiUser>('/api/auth/me')
  return mapUser(user)
}

export async function updateCurrentUser(req: UpdateCurrentUserRequest): Promise<UserProfile> {
  const user = await apiPatch<ApiUser>(
    '/api/auth/me',
    {
      display_name: req.displayName,
      username: req.username,
      avatar_url: req.avatarUrl ?? null
    }
  )
  return mapUser(user)
}
