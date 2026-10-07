<template>
  <div
    class="app-shell"
    :class="{ 'app-shell--project': isProjectRoute, 'app-shell--sidebar-collapsed': isSidebarCollapsed }"
    :style="shellStyle"
  >
    <Sidebar
      :force-visible="isProjectRoute"
      :collapsed="isSidebarCollapsed"
      @toggle-collapse="toggleSidebar"
    />
    <div
      v-if="!isSidebarCollapsed"
      class="sidebar-resize-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label="调整侧边栏宽度"
      tabindex="0"
      @pointerdown="startSidebarResize"
      @keydown="resizeSidebarFromKeyboard"
    />
    <main class="app-main" :style="projectMainStyle">
      <slot />
    </main>
  </div>

  <Teleport to="body">
    <div
      v-if="showModelConfigRequiredDialog"
      class="delete-confirm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="model-config-required-title"
    >
      <div class="delete-confirm__dialog">
        <h2 id="model-config-required-title" class="delete-confirm__title">需要配置模型</h2>
        <p class="delete-confirm__body">
          当前账号还没有可用的模型配置。
        </p>
        <p class="delete-confirm__hint">请先在系统设置中配置模型 API 地址、密钥和模型名称后再使用 TestAgent。</p>
        <div class="delete-confirm__actions">
          <button class="delete-confirm__button delete-confirm__button--primary" type="button" @click="goToSettings">
            去设置
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Sidebar from './Sidebar.vue'
import { useSettingsStore } from '@/stores/settingsStore'

const SIDEBAR_MIN_WIDTH = 256
const SIDEBAR_MAX_WIDTH = 640
const SIDEBAR_RAIL_WIDTH = 54
const SIDEBAR_COLLAPSED_STORAGE_KEY = 'testagent.sidebar.collapsed'
// AppLayout is owned by each route component, so these refs deliberately live at module scope.
// They preserve the user's compact preference while navigating between application views.
const sidebarWidth = ref(SIDEBAR_MIN_WIDTH)
const isSidebarCollapsed = ref(readCollapsedSidebarPreference())

function readCollapsedSidebarPreference() {
  return typeof window !== 'undefined' && window.sessionStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === 'true'
}

const route = useRoute()
const router = useRouter()
const settingsStore = useSettingsStore()
const showModelConfigRequiredDialog = ref(false)
const checkingModelSettings = ref(false)
const isProjectRoute = computed(() => route.path.startsWith('/projects'))
const bypassModelSettingsGate = computed(() => route.name === 'settings' || route.path.startsWith('/help'))
const projectShellStyle = computed(() => isProjectRoute.value ? { display: 'flex', height: '100vh', minHeight: '0' } : undefined)
const projectMainStyle = computed(() => isProjectRoute.value ? { height: '100%', minHeight: '0' } : undefined)
const shellStyle = computed(() => ({
  ...projectShellStyle.value,
  '--ta-sidebar-layout-width': `${isSidebarCollapsed.value ? SIDEBAR_RAIL_WIDTH : sidebarWidth.value}px`
}))

function clampSidebarWidth(nextWidth: number) {
  const viewportLimit = typeof window === 'undefined' ? SIDEBAR_MAX_WIDTH : Math.max(SIDEBAR_MIN_WIDTH, Math.floor(window.innerWidth * 0.48))
  return Math.min(Math.max(nextWidth, SIDEBAR_MIN_WIDTH), Math.min(SIDEBAR_MAX_WIDTH, viewportLimit))
}

function toggleSidebar() {
  isSidebarCollapsed.value = !isSidebarCollapsed.value
  if (typeof window !== 'undefined') {
    window.sessionStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(isSidebarCollapsed.value))
  }
}

function startSidebarResize(event: PointerEvent) {
  const handle = event.currentTarget as HTMLElement
  handle.setPointerCapture(event.pointerId)
  document.body.classList.add('is-resizing-sidebar')

  const updateWidth = (pointerEvent: PointerEvent) => {
    sidebarWidth.value = clampSidebarWidth(pointerEvent.clientX)
  }
  const stopResize = (pointerEvent: PointerEvent) => {
    if (handle.hasPointerCapture(pointerEvent.pointerId)) handle.releasePointerCapture(pointerEvent.pointerId)
    document.body.classList.remove('is-resizing-sidebar')
    handle.removeEventListener('pointermove', updateWidth)
    handle.removeEventListener('pointerup', stopResize)
    handle.removeEventListener('pointercancel', stopResize)
  }

  updateWidth(event)
  handle.addEventListener('pointermove', updateWidth)
  handle.addEventListener('pointerup', stopResize)
  handle.addEventListener('pointercancel', stopResize)
}

function resizeSidebarFromKeyboard(event: KeyboardEvent) {
  const step = event.shiftKey ? 32 : 16
  if (event.key === 'ArrowLeft') {
    event.preventDefault()
    sidebarWidth.value = clampSidebarWidth(sidebarWidth.value - step)
  }
  if (event.key === 'ArrowRight') {
    event.preventDefault()
    sidebarWidth.value = clampSidebarWidth(sidebarWidth.value + step)
  }
}

async function checkModelSettingsBeforeUse() {
  if (bypassModelSettingsGate.value) {
    showModelConfigRequiredDialog.value = false
    return
  }
  if (checkingModelSettings.value || showModelConfigRequiredDialog.value) return

  checkingModelSettings.value = true
  try {
    const ready = await settingsStore.ensureModelSettings()
    showModelConfigRequiredDialog.value = !ready && route.name !== 'settings'
  } finally {
    checkingModelSettings.value = false
  }
}

function goToSettings() {
  showModelConfigRequiredDialog.value = false
  router.push('/settings')
}

watch(
  () => route.fullPath,
  () => {
    void checkModelSettingsBeforeUse()
  },
  { immediate: true }
)

onBeforeUnmount(() => {
  document.body.classList.remove('is-resizing-sidebar')
})
</script>

<style scoped>
.app-shell {
  display: flex;
  width: 100%;
  height: 100vh;
  overflow: hidden;
  background: var(--ta-background);
}

.app-main {
  position: relative;
  display: flex;
  flex: 1;
  min-width: 0;
  height: 100%;
  overflow: hidden;
}

.sidebar-resize-handle {
  position: relative;
  z-index: 3;
  flex: 0 0 8px;
  width: 8px;
  margin-inline: -4px;
  cursor: col-resize;
  touch-action: none;
  outline: none;
}

.sidebar-resize-handle::after {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 1px;
  content: '';
  background: transparent;
  transition: background 140ms ease;
}

.sidebar-resize-handle:hover::after,
.sidebar-resize-handle:focus-visible::after {
  background: #a6a6a6;
}

.sidebar-resize-handle:focus-visible {
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.24);
}

:global(body.is-resizing-sidebar) {
  cursor: col-resize;
  user-select: none;
}

.delete-confirm {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  padding: 24px;
  background: rgba(248, 249, 255, 0.72);
  backdrop-filter: blur(2px);
  place-items: center;
}

.delete-confirm__dialog {
  width: min(480px, 100%);
  padding: 22px 22px 18px;
  color: var(--ta-text);
  background: #ffffff;
  border: 1px solid rgba(194, 198, 216, 0.9);
  border-radius: 14px;
  box-shadow:
    0 18px 48px rgba(15, 23, 42, 0.14),
    0 2px 8px rgba(15, 23, 42, 0.08);
}

.delete-confirm__title {
  margin: 0 0 18px;
  color: var(--ta-text-strong);
  font-size: 20px;
  font-weight: 650;
  line-height: 28px;
}

.delete-confirm__body {
  margin: 0;
  color: var(--ta-text-strong);
  font-size: 15px;
  line-height: 24px;
}

.delete-confirm__hint {
  margin: 8px 0 28px;
  color: var(--ta-text-muted);
  font-size: 14px;
  line-height: 22px;
}

.delete-confirm__actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}

.delete-confirm__button {
  min-width: 64px;
  height: 40px;
  padding: 0 18px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  border-radius: 999px;
  transition:
    background 160ms ease,
    border-color 160ms ease,
    box-shadow 160ms ease;
}

.delete-confirm__button--primary {
  color: #ffffff;
  background: var(--ta-primary);
  border: 1px solid var(--ta-primary);
}

.delete-confirm__button--primary:hover {
  background: var(--ta-primary-container);
  border-color: var(--ta-primary-container);
}

@media (max-width: 860px) {
  .app-shell {
    display: block;
    height: auto;
    min-height: 100vh;
  }

  .app-main {
    min-height: 100vh;
  }

  .app-shell--project {
    display: flex;
    height: 100vh;
    min-height: 0;
  }

  .app-shell--project .app-main {
    height: 100%;
    min-height: 0;
  }

  .app-shell--project :deep(.sidebar) {
    display: flex;
  }

  .sidebar-resize-handle {
    display: none;
  }
}
</style>
