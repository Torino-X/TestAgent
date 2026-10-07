<template>
  <aside
    class="sidebar"
    :class="{ 'sidebar--collapsed': collapsed }"
    :style="shouldForceVisible ? { display: 'flex' } : undefined"
  >
    <div class="sidebar__header" :class="{ 'sidebar__header--with-history-divider': showHistoryDivider }">
      <button
        v-if="!collapsed"
        class="sidebar-collapse-button sidebar-tooltip"
        type="button"
        style="position: absolute; top: 16px; right: 16px; bottom: auto; left: auto; z-index: 2; margin: 0; transform: none;"
        aria-label="收起侧边栏"
        data-tooltip="收起侧边栏"
        @click="emit('toggle-collapse')"
      >
        <img class="sidebar-collapse-button__icon" :src="sidebarToggleIcon" alt="" aria-hidden="true" />
      </button>

      <button
        v-if="collapsed"
        class="brand brand--collapsed sidebar-tooltip"
        type="button"
        aria-label="展开侧边栏"
        data-tooltip="展开侧边栏"
        @click="emit('toggle-collapse')"
      >
        <span class="brand__mark">
          <n-icon :component="BugOutline" :size="21" />
        </span>
      </button>

      <div v-if="!collapsed" class="brand">
        <div class="brand__mark">
          <n-icon :component="BugOutline" :size="22" />
        </div>
        <div>
          <div class="brand__name">TestAgent</div>
          <div class="brand__caption">AI 测试工程师</div>
        </div>
      </div>

      <n-button v-if="!collapsed" class="new-task" type="primary" block @click="goNewChat">
        <template #icon>
          <n-icon :component="Add" />
        </template>
        新建任务
      </n-button>
      <RouterLink v-if="!collapsed" class="sidebar-navigation__item sidebar-navigation__item--library" to="/library">
        <img class="sidebar-navigation__icon" :src="libraryIcon" alt="" aria-hidden="true" />
        <span>资料库</span>
      </RouterLink>
      <RouterLink v-if="!collapsed" class="sidebar-navigation__item sidebar-navigation__item--projects" to="/projects">
        <img class="sidebar-navigation__icon" :src="projectIcon" alt="" aria-hidden="true" />
        <span>项目</span>
      </RouterLink>
      <RouterLink v-if="!collapsed" class="sidebar-navigation__item sidebar-navigation__item--templates" to="/templates">
        <img class="sidebar-navigation__icon" :src="templateMarketIcon" alt="" aria-hidden="true" />
        <span>模板市场</span>
      </RouterLink>
    </div>

    <div v-if="collapsed" class="sidebar__compact-navigation" aria-label="主要导航">
      <RouterLink class="sidebar-navigation__item sidebar-tooltip" to="/library" aria-label="资料库" data-tooltip="资料库">
        <img class="sidebar-navigation__icon" :src="libraryIcon" alt="" aria-hidden="true" />
      </RouterLink>
      <RouterLink class="sidebar-navigation__item sidebar-tooltip" to="/projects" aria-label="项目" data-tooltip="项目">
        <img class="sidebar-navigation__icon" :src="projectIcon" alt="" aria-hidden="true" />
      </RouterLink>
      <RouterLink class="sidebar-navigation__item sidebar-tooltip" to="/templates" aria-label="模板市场" data-tooltip="模板市场">
        <img class="sidebar-navigation__icon" :src="templateMarketIcon" alt="" aria-hidden="true" />
      </RouterLink>
      <button
        class="sidebar-navigation__item sidebar-tooltip sidebar-navigation__recent"
        :class="{ 'sidebar-navigation__recent--active': isRecentPopoverOpen }"
        type="button"
        aria-label="最近"
        aria-haspopup="dialog"
        :aria-expanded="isRecentPopoverOpen"
        data-tooltip="最近"
        @click="toggleRecentPopover"
      >
        <img class="sidebar-navigation__icon" :src="recentIcon" alt="" aria-hidden="true" />
      </button>
    </div>

    <section v-if="collapsed && isRecentPopoverOpen" class="sidebar-recent-popover" aria-label="最近对话">
      <div class="sidebar-recent-popover__title">最近</div>
      <div class="sidebar-recent-popover__list">
        <RouterLink
          v-for="conversation in recentConversations"
          :key="conversation.id"
          class="sidebar-recent-popover__item"
          :class="{ 'sidebar-recent-popover__item--active': conversation.id === conversationStore.activeConversationId }"
          :to="`/chat/${conversation.id}`"
          :title="conversation.title"
          @click="isRecentPopoverOpen = false"
        >
          {{ conversation.title }}
        </RouterLink>
        <p v-if="recentConversations.length === 0" class="sidebar-recent-popover__empty">暂无最近对话</p>
      </div>
    </section>

    <nav class="sidebar__navigation" aria-label="主要导航">
      <RouterLink class="sidebar-navigation__item" to="/library">
        <img class="sidebar-navigation__icon" :src="libraryIcon" alt="" aria-hidden="true" />
        <span>资料库</span>
      </RouterLink>
    </nav>

    <ConversationList
      v-if="!collapsed"
      class="sidebar__conversations"
      :conversations="conversationStore.conversations"
      :active-id="conversationStore.activeConversationId"
      @overflow-change="showHistoryDivider = $event"
      @delete="handleDeleteConversation"
      @rename="handleRenameConversation"
      @move-to-project="moveConversationTarget = $event"
    />

    <div class="sidebar__footer">
      <div v-if="isUserMenuOpen" class="sidebar-user-menu" role="menu" aria-label="用户操作菜单">
        <a
          class="sidebar-user-menu__item"
          href="/help"
          target="_blank"
          rel="noopener"
          role="menuitem"
          @click="isUserMenuOpen = false"
        >
          <n-icon :component="HelpCircleOutline" :size="16" />
          <span>帮助</span>
        </a>
        <RouterLink v-if="false" class="sidebar-user-menu__item" to="/settings" role="menuitem">
          <n-icon :component="BookOutline" :size="16" />
          <span>知识库</span>
        </RouterLink>
        <RouterLink class="sidebar-user-menu__item" to="/settings" role="menuitem">
          <n-icon :component="SettingsOutline" :size="16" />
          <span>设置</span>
        </RouterLink>
        <button class="sidebar-user-menu__item" type="button" role="menuitem" @click="showProfileEditor = true">
          <n-icon :component="PersonOutline" :size="16" />
          <span>个人资料</span>
        </button>
        <div class="sidebar-user-menu__divider" aria-hidden="true"></div>
        <button class="sidebar-user-menu__item sidebar-user-menu__item--logout" type="button" role="menuitem" @click="handleLogout">
          <n-icon :component="LogOutOutline" :size="16" />
          <span>退出登录</span>
        </button>
      </div>

      <button
        class="sidebar-user-trigger sidebar-tooltip"
        :class="{ 'sidebar-user-trigger--active': isUserMenuOpen }"
        type="button"
        :aria-expanded="isUserMenuOpen"
        aria-haspopup="menu"
        :aria-label="collapsed ? userDisplayName : undefined"
        :data-tooltip="collapsed ? userDisplayName : undefined"
        @click="toggleUserMenu"
      >
        <span class="sidebar-user-avatar" aria-hidden="true">
          <img v-if="currentUser?.avatarUrl" :src="currentUser.avatarUrl" alt="" />
          <span v-else>{{ avatarInitials }}</span>
        </span>
        <span class="sidebar-user-name" :title="userDisplayName">{{ userDisplayName }}</span>
      </button>
    </div>

    <ProfileEditorModal :show="showProfileEditor" @close="showProfileEditor = false" />
    <MoveConversationDialog
      :show="moveConversationTarget !== null"
      :conversation="moveConversationTarget"
      :projects="projectStore.projects"
      :saving="projectStore.loading.mutating"
      @close="moveConversationTarget = null"
      @move="moveConversation"
    />
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NIcon, useMessage } from 'naive-ui'
import {
  Add,
  BookOutline,
  BugOutline,
  HelpCircleOutline,
  LogOutOutline,
  PersonOutline,
  SettingsOutline
} from '@vicons/ionicons5'
import ConversationList from './ConversationList.vue'
import ProfileEditorModal from '@/components/profile/ProfileEditorModal.vue'
import { useConversationStore } from '@/stores/conversationStore'
import { useAuthStore } from '@/stores/authStore'
import { useProjectStore } from '@/stores/projectStore'
import MoveConversationDialog from '@/components/projects/MoveConversationDialog.vue'
import libraryIcon from '@/assets/Sidebar-svg/资料库.svg'
import projectIcon from '@/assets/Sidebar-svg/项目.svg'
import templateMarketIcon from '@/assets/Sidebar-svg/模板市场.svg'
import sidebarToggleIcon from '@/assets/icons/侧边栏展开与收起.svg'
import recentIcon from '@/assets/icons/最近.svg'
import type { Conversation } from '@/types'

const route = useRoute()
const router = useRouter()
const props = withDefaults(defineProps<{ forceVisible?: boolean; collapsed?: boolean }>(), { forceVisible: false, collapsed: false })
const emit = defineEmits<{ (event: 'toggle-collapse'): void }>()
const shouldForceVisible = computed(() => props.forceVisible || route.path.startsWith('/projects') || route.path.startsWith('/templates'))
const collapsed = computed(() => props.collapsed)
const conversationStore = useConversationStore()
const authStore = useAuthStore()
const projectStore = useProjectStore()
const message = useMessage()
const currentUser = computed(() => authStore.user)
const showProfileEditor = ref(false)
const isUserMenuOpen = ref(false)
const isRecentPopoverOpen = ref(false)
const moveConversationTarget = ref<Conversation | null>(null)
const showHistoryDivider = ref(false)

const recentConversations = computed(() =>
  conversationStore.conversations.filter((conversation) => conversation.id !== 'conv_new')
)

const userDisplayName = computed(() => {
  const user = currentUser.value
  return user?.name || user?.username || '用户'
})

const avatarInitials = computed(() => {
  const source = userDisplayName.value.trim()
  if (!source) return '用'
  const letters = Array.from(source.replace(/\s+/g, ''))
  return letters.slice(0, 2).join('').toUpperCase()
})

function toggleUserMenu() {
  isUserMenuOpen.value = !isUserMenuOpen.value
}

function toggleRecentPopover() {
  isRecentPopoverOpen.value = !isRecentPopoverOpen.value
}

async function goNewChat() {
  const target = await conversationStore.startNewConversation()
  router.push(`/chat/${target.id}`)
}

async function handleLogout() {
  await authStore.logout()
  router.push('/login')
}

async function handleDeleteConversation(id: string) {
  const isActive = conversationStore.activeConversationId === id
  await conversationStore.deleteConversation(id)
  if (isActive || router.currentRoute.value.params.conversationId === id) {
    router.push('/chat')
  }
}

async function handleRenameConversation({ id, title }: { id: string; title: string }) {
  await conversationStore.updateConversationTitle(id, title)
}

async function moveConversation(projectId: string) {
  const conversation = moveConversationTarget.value
  if (!conversation) return
  try {
    await projectStore.moveConversationToProject(projectId, { id: conversation.id })
    const project = projectStore.projects.find((item) => item.id === projectId)
    if (project) conversationStore.setConversationProject(conversation.id, { id: project.id, name: project.name })
    moveConversationTarget.value = null
    message.success('聊天已移动到项目')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '移动聊天失败')
  }
}

onMounted(() => { void projectStore.fetchProjects().catch(() => undefined) })
</script>

<style scoped>
.sidebar {
  position: relative;
  display: flex;
  flex: 0 0 var(--ta-sidebar-layout-width, var(--ta-sidebar-width));
  flex-direction: column;
  width: var(--ta-sidebar-layout-width, var(--ta-sidebar-width));
  height: 100%;
  color: var(--text-body);
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  background: var(--ta-surface);
  border-right: 1px solid var(--ta-border);
  box-shadow: 0 2px 16px rgba(17, 24, 39, 0.03);
}

.sidebar__header {
  position: relative;
  padding: 24px 10px 10px;
}

.sidebar__header--with-history-divider {
  border-bottom: 1px solid var(--ta-border);
}

.brand {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 20px;
  margin-left: 14px;
  margin-right: 14px;
}

.sidebar-collapse-button {
  position: absolute !important;
  top: 16px !important;
  right: 16px !important;
  bottom: auto !important;
  left: auto !important;
  z-index: 2;
  display: grid;
  width: 30px;
  height: 30px;
  padding: 0;
  margin: 0 !important;
  color: var(--ta-text-muted);
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: var(--ta-radius-sm);
  transform: none !important;
  place-items: center;
}

.sidebar-collapse-button__icon {
  width: 18px;
  height: 18px;
  opacity: 0.72;
}

.sidebar-collapse-button:hover,
.sidebar-collapse-button:focus-visible {
  color: var(--ta-text-strong);
  background: var(--ta-surface-low);
  outline: none;
}

.brand__mark {
  display: grid;
  width: 40px;
  height: 40px;
  color: #ffffff;
  place-items: center;
  background: var(--ta-primary-container);
  border-radius: var(--ta-radius-md);
}

.brand__name {
  color: var(--ta-text-strong);
  font-size: var(--text-card-title);
  font-weight: var(--font-semibold);
  line-height: var(--leading-ui);
}

.brand__caption {
  color: var(--text-tertiary);
  font-size: var(--text-meta);
  font-weight: var(--font-regular);
  line-height: var(--leading-meta);
}

.new-task :deep(.n-button__content) {
  gap: 6px;
}

.new-task {
  box-sizing: border-box;
  width: 210px;
  max-width: calc(100% - 28px);
  margin: 0 14px;
}

.sidebar__navigation {
  display: none;
}

.sidebar__compact-navigation {
  display: none;
}

.sidebar-navigation__item {
  display: flex;
  align-items: center;
  height: 36px;
  padding: 0 8px;
  gap: 8px;
  color: var(--ta-text-strong);
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  border-radius: var(--ta-radius-sm);
  transition: background 160ms ease;
}

.sidebar-navigation__item:hover,
.sidebar-navigation__item.router-link-active {
  color: var(--ta-text-strong);
  background: #ececec;
}

.sidebar-navigation__item .n-icon {
  color: var(--ta-text-muted);
}

.sidebar-navigation__icon {
  flex: 0 0 18px;
  width: 18px;
  height: 18px;
  object-fit: contain;
  opacity: 0.78;
}

.sidebar__header .sidebar-navigation__item--library {
  margin-top: 12px;
}

.sidebar__footer {
  display: flex;
  flex-direction: column;
  position: relative;
  z-index: 1;
  margin-top: auto;
  padding: 16px;
}

.sidebar__conversations {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}

.sidebar-user-trigger {
  display: flex;
  align-items: center;
  width: 100%;
  height: 48px;
  padding: 8px;
  gap: 10px;
  color: #111827;
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 8px;
  transition:
    background 160ms ease;
}

.sidebar-user-trigger:hover,
.sidebar-user-trigger--active {
  background: #f3f4f6;
}

.sidebar-user-trigger:focus-visible,
.sidebar-user-menu__item:focus-visible {
  outline: 2px solid var(--ta-primary);
  outline-offset: 2px;
}

.sidebar-user-avatar {
  display: grid;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  overflow: hidden;
  color: #0054cd;
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  line-height: var(--leading-xs);
  place-items: center;
  background: #cfe5ff;
  border-radius: 50%;
}

.sidebar-user-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.sidebar-user-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-user-menu {
  position: absolute;
  bottom: 72px;
  left: 16px;
  z-index: 10;
  width: min(228px, calc(100% - 32px));
  padding: 8px;
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(17, 24, 39, 0.1);
}

.sidebar-user-menu__item {
  display: flex;
  align-items: center;
  width: 100%;
  height: 40px;
  padding: 9px 12px;
  gap: 12px;
  color: #111827;
  font: inherit;
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 6px;
  transition: background 160ms ease;
}

.sidebar-user-menu__item:hover,
.sidebar-user-menu__item.router-link-active {
  color: #111827;
  background: #f3f4f6;
}

.sidebar-user-menu__item .n-icon {
  flex: 0 0 16px;
  color: #6b7280;
}

.sidebar-user-menu__item--logout {
  color: #111827;
}

.sidebar-user-menu__divider {
  height: 1px;
  margin: 4px 0;
  background: #e5e7eb;
}

.sidebar--collapsed {
  overflow: visible;
}

.sidebar--collapsed .sidebar__header {
  padding: 12px 7px 6px;
}

.sidebar--collapsed .sidebar__header--with-history-divider {
  border-bottom: 0;
}

.sidebar--collapsed .brand:not(.brand--collapsed),
.sidebar--collapsed .new-task,
.sidebar--collapsed .sidebar__header > .sidebar-navigation__item,
.sidebar--collapsed .sidebar__navigation,
.sidebar--collapsed .sidebar__conversations {
  display: none;
}

.sidebar--collapsed .brand--collapsed {
  display: grid;
  width: 40px;
  height: 40px;
  padding: 0;
  margin: 0 auto 8px;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: var(--ta-radius-md);
  place-items: center;
}

.sidebar--collapsed .brand--collapsed:hover,
.sidebar--collapsed .brand--collapsed:focus-visible {
  background: var(--ta-surface-low);
  outline: none;
}

.sidebar--collapsed .brand--collapsed .brand__mark {
  width: 32px;
  height: 32px;
}

.sidebar--collapsed .sidebar__compact-navigation {
  display: grid;
  gap: 4px;
  padding: 0 4px;
}

.sidebar--collapsed .sidebar-navigation__recent--active {
  background: var(--ta-surface-low);
}

.sidebar--collapsed .sidebar-navigation__recent {
  font: inherit;
  cursor: pointer;
  background: transparent;
  border: 0;
  outline: 0;
}

.sidebar--collapsed .sidebar-navigation__recent--active {
  background: var(--ta-surface-low);
}

.sidebar--collapsed .sidebar-navigation__item {
  justify-content: center;
  width: 40px;
  height: 40px;
  padding: 0;
  margin: 0 auto;
}

.sidebar--collapsed .sidebar-navigation__item .sidebar-navigation__icon {
  flex-basis: 19px;
  width: 19px;
  height: 19px;
}

.sidebar--collapsed .sidebar__footer {
  z-index: 20;
  padding: 8px 7px 12px;
}

.sidebar--collapsed .sidebar-user-trigger {
  justify-content: center;
  width: 40px;
  height: 40px;
  padding: 6px;
  margin: 0 auto;
}

.sidebar--collapsed .sidebar-user-name {
  display: none;
}

.sidebar--collapsed .sidebar-user-menu {
  position: fixed;
  z-index: 21;
  bottom: 56px;
  left: 62px;
  width: 228px;
  max-height: calc(100vh - 72px);
  overflow-y: auto;
}

.sidebar-recent-popover {
  position: absolute;
  top: 188px;
  left: calc(100% - 12px);
  z-index: 20;
  width: 264px;
  max-height: min(360px, calc(100vh - 220px));
  padding: 12px 0 8px;
  overflow: hidden;
  background: #ffffff;
  border: 1px solid var(--ta-border);
  border-radius: 14px;
  box-shadow: 0 12px 32px rgba(17, 24, 39, 0.12);
}

.sidebar-recent-popover__title {
  padding: 0 16px 8px;
  color: var(--text-tertiary);
  font-size: var(--text-meta);
  line-height: var(--leading-meta);
}

.sidebar-recent-popover__list {
  max-height: calc(min(360px, 100vh - 220px) - 40px);
  padding: 0 8px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: #d1d1d1 transparent;
}

.sidebar-recent-popover__list::-webkit-scrollbar {
  width: 5px;
}

.sidebar-recent-popover__list::-webkit-scrollbar-thumb {
  background: #d1d1d1;
  border-radius: 999px;
}

.sidebar-recent-popover__item {
  display: block;
  padding: 8px;
  overflow: hidden;
  color: var(--ta-text-strong);
  font-size: var(--text-ui);
  line-height: var(--leading-ui);
  text-overflow: ellipsis;
  white-space: nowrap;
  border-radius: var(--ta-radius-sm);
}

.sidebar-recent-popover__item:hover,
.sidebar-recent-popover__item--active {
  background: #ececec;
}

.sidebar-recent-popover__empty {
  padding: 10px 8px;
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--text-ui);
  line-height: var(--leading-ui);
}

.sidebar-tooltip {
  position: relative;
}

.sidebar--collapsed .sidebar-tooltip[data-tooltip]::after {
  position: absolute;
  top: 50%;
  left: calc(100% + 8px);
  z-index: 30;
  padding: 6px 9px;
  color: #ffffff;
  font-size: 12px;
  font-weight: 500;
  line-height: 16px;
  white-space: nowrap;
  pointer-events: none;
  content: attr(data-tooltip);
  visibility: hidden;
  background: #202020;
  border-radius: 6px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.16);
  opacity: 0;
  transform: translateY(-50%) translateX(-2px);
  transition: opacity 120ms ease, transform 120ms ease, visibility 120ms ease;
}

.sidebar--collapsed .sidebar-tooltip[data-tooltip]:hover::after,
.sidebar--collapsed .sidebar-tooltip[data-tooltip]:focus-visible::after {
  visibility: visible;
  opacity: 1;
  transform: translateY(-50%) translateX(0);
}

@media (max-width: 860px) {
  .sidebar {
    display: none;
  }
}
</style>
