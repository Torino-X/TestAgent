import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { renderToString } from 'vue/server-renderer'
import { NMessageProvider } from 'naive-ui'
import Sidebar from './Sidebar.vue'
import source from './Sidebar.vue?raw'
import conversationListSource from './ConversationList.vue?raw'
import { routes } from '@/router/routes'

function okResponse(data: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify({ code: 0, message: 'success', data }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    })
  )
}

async function renderSidebar() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({
    history: createMemoryHistory(),
    routes
  })
  await router.push('/')
  await router.isReady()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse({ conversations: [], total: 0 })))

  const app = createSSRApp({
    render: () =>
      h(NMessageProvider, null, {
        default: () => h(Sidebar)
      })
  })
  app.use(pinia)
  app.use(router)
  return renderToString(app)
}

describe('Sidebar context productization', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('removes the ordinary user Context management entry', async () => {
    const html = await renderSidebar()

    expect(html).not.toContain('Context 管理')
    expect(html).not.toContain('href="/context"')
  })

  it('places the template market entry after projects and uses the supplied sidebar SVG set', () => {
    const projects = source.indexOf('sidebar-navigation__item--projects')
    const templates = source.indexOf('sidebar-navigation__item--templates')
    expect(templates).toBeGreaterThan(projects)
    expect(source).toContain("@/assets/Sidebar-svg/资料库.svg")
    expect(source).toContain("@/assets/Sidebar-svg/项目.svg")
    expect(source).toContain("@/assets/Sidebar-svg/模板市场.svg")
    expect(source).not.toContain(':component="LibraryOutline"')
    expect(source).not.toContain(':component="FolderOutline"')
    expect(source).toContain('to="/templates"')
  })

  it('removes the leading chat icon from pinned and recent conversation rows', () => {
    expect(conversationListSource).not.toContain('ChatbubbleOutline')
    expect(conversationListSource).not.toContain('class="conversation-item__icon"')
    expect(conversationListSource).toContain('conversation-item__label')
  })
})

describe('Sidebar bottom user menu redesign', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders only the user card actions in the collapsed footer', async () => {
    const html = await renderSidebar()

    expect(html).toContain('sidebar-user-trigger')
    expect(html).toContain('sidebar-user-avatar')
    expect(html).toContain('sidebar-user-name')
    expect(html).not.toContain('Plus')
    expect(html).not.toContain('套餐')
    expect(html).not.toContain('sidebar-link sidebar-link--subtle')
  })

  it('does not draw a separator line above the collapsed user card', () => {
    const footerRule = source.match(/\.sidebar__footer\s*{[\s\S]*?}/)?.[0] ?? ''

    expect(footerRule).not.toContain('border-top')
  })

  it('uses local Sidebar state for the user menu', () => {
    expect(source).toContain('const isUserMenuOpen = ref(false)')
  })

  it('opens and closes the menu from the user trigger toggle', () => {
    expect(source).toContain('function toggleUserMenu()')
    expect(source).toContain('isUserMenuOpen.value = !isUserMenuOpen.value')
    expect(source).toContain('@click="toggleUserMenu"')
  })

  it('keeps outside click close disabled', () => {
    expect(source).not.toContain('document.addEventListener')
    expect(source).not.toContain('window.addEventListener')
    expect(source).not.toContain('onClickoutside')
    expect(source).not.toContain('clickoutside')
  })

  it('keeps the menu hidden until the trigger opens it', () => {
    expect(source).toContain('v-if="isUserMenuOpen"')
    expect(source).toContain('aria-haspopup="menu"')
    expect(source).toContain(':aria-expanded="isUserMenuOpen"')
  })

  it('preserves the five existing menu actions without adding ChatGPT-only entries', () => {
    expect(source).toContain('<span>帮助</span>')
    expect(source).toContain('to="/knowledge"')
    expect(source).toContain('to="/settings"')
    expect(source).toContain('showProfileEditor = true')
    expect(source).toContain('handleLogout')
    expect(source).not.toContain('升级套餐')
    expect(source).not.toContain('Plus')
    expect(source).not.toContain('订阅')
    expect(source).not.toContain('商店')
  })

  it('opens the existing help action in a separate browser tab', () => {
    expect(source).toContain('<a\n          class="sidebar-user-menu__item"')
    expect(source).toContain('href="/help"')
    expect(source).toContain('target="_blank"')
    expect(source).toContain('rel="noopener"')
    expect(source).not.toContain('<RouterLink class="sidebar-user-menu__item" to="/help"')
    expect(source).toContain('<span>帮助</span>')
  })

  it('keeps the knowledge route unchanged', () => {
    expect(source).toContain('<RouterLink class="sidebar-user-menu__item" to="/knowledge"')
  })

  it('keeps the settings route unchanged', () => {
    expect(source).toContain('<RouterLink class="sidebar-user-menu__item" to="/settings"')
  })

  it('keeps the profile editor behavior unchanged', () => {
    expect(source).toContain('@click="showProfileEditor = true"')
    expect(source).toContain('<ProfileEditorModal :show="showProfileEditor" @close="showProfileEditor = false" />')
  })

  it('keeps the logout flow unchanged', () => {
    expect(source).toContain('await authStore.logout()')
    expect(source).toContain("router.push('/login')")
    expect(source).toContain('@click="handleLogout"')
  })

  it('uses the authenticated user profile for name and avatar initials', () => {
    expect(source).toContain('const currentUser = computed(() => authStore.user)')
    expect(source).toContain('userDisplayName')
    expect(source).toContain('avatarInitials')
    expect(source).not.toContain("'XiaoYun'")
    expect(source).not.toContain('"XiaoYun"')
  })

  it('supports real avatar URLs without replacing the user data source', () => {
    expect(source).toContain('v-if="currentUser?.avatarUrl"')
    expect(source).toContain(':src="currentUser.avatarUrl"')
  })

  it('matches the approved Stitch geometry for the trigger and floating menu', () => {
    expect(source).toContain('width: 228px;')
    expect(source).toContain('height: 48px;')
    expect(source).toContain('border-radius: 8px;')
    expect(source).toContain('width: 28px;')
    expect(source).toContain('bottom: 72px;')
    expect(source).toContain('border-radius: 12px;')
    expect(source).toContain('box-shadow: 0 12px 32px rgba(17, 24, 39, 0.1);')
  })

  it('keeps long usernames constrained with ellipsis', () => {
    expect(source).toContain('white-space: nowrap;')
    expect(source).toContain('overflow: hidden;')
    expect(source).toContain('text-overflow: ellipsis;')
    expect(source).toContain('min-width: 0;')
  })

  it('keeps the conversation list isolated from the floating menu layout', () => {
    expect(source).toContain('position: absolute;')
    expect(source).toContain('z-index: 10;')
    expect(source).toContain('margin-top: auto;')
    expect(source).toContain('overflow: hidden;')
  })

  it('keeps the top Sidebar and new task behavior untouched', () => {
    expect(source).toContain('<div class="brand__name">TestAgent</div>')
    expect(source).toContain('<n-button v-if="!collapsed" class="new-task" type="primary" block @click="goNewChat">')
    expect(source).toContain('conversationStore.startNewConversation()')
    expect(source).toContain('router.push(`/chat/${target.id}`)')
  })
})
