/**
 * Auth store — login state, current user, token.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { UserProfile } from '@/types'
import * as authApi from '@/api/authApi'
import { clearAuthToken, getAuthToken } from '@/api/request'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserProfile | null>(null)
  const token = ref<string | null>(getAuthToken())

  const isAuthenticated = computed(() => !!token.value)

  async function login(account: string, password: string) {
    const res = await authApi.login({ account, password })
    token.value = res.token
    user.value = res.user
  }

  async function register(email: string, password: string, confirmPassword: string, agreeTerms: boolean) {
    const res = await authApi.register({ email, password, confirmPassword, agreeTerms })
    token.value = res.token
    user.value = res.user
  }

  async function logout() {
    await authApi.logout()
    token.value = null
    user.value = null
  }

  async function fetchCurrentUser() {
    user.value = await authApi.fetchCurrentUser()
    return user.value
  }

  async function restoreSession() {
    if (!token.value) return false
    try {
      user.value = await authApi.fetchCurrentUser()
      return true
    } catch {
      clearAuthToken()
      token.value = null
      user.value = null
      return false
    }
  }

  async function updateCurrentUser(payload: authApi.UpdateCurrentUserRequest) {
    user.value = await authApi.updateCurrentUser(payload)
    return user.value
  }

  return {
    user,
    token,
    isAuthenticated,
    login,
    register,
    logout,
    restoreSession,
    fetchCurrentUser,
    updateCurrentUser
  }
})
