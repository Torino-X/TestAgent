import { createRouter, createWebHistory } from 'vue-router'
import { routes } from './routes'
import { useAuthStore } from '@/stores/authStore'

export const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach(async (to, _from, next) => {
  const authStore = useAuthStore()

  const publicPaths = ['/login', '/register']
  const isPublic = publicPaths.includes(to.path)

  if (authStore.isAuthenticated && !authStore.user) {
    const restored = await authStore.restoreSession()
    if (!restored && !isPublic) {
      next({ path: '/login', query: { redirect: to.fullPath } })
      return
    }
  }

  if (!authStore.isAuthenticated && !isPublic) {
    // Not logged in → redirect to login
    next({ path: '/login', query: { redirect: to.fullPath } })
  } else if (authStore.isAuthenticated && publicPaths.includes(to.path)) {
    // Already logged in → redirect to main workspace
    next('/chat')
  } else {
    next()
  }
})
