import { describe, expect, it } from 'vitest'
import layoutSource from './AppLayout.vue?raw'
import sidebarSource from './Sidebar.vue?raw'
import conversationListSource from './ConversationList.vue?raw'

describe('Sidebar interaction shell', () => {
  it('keeps the current 256px baseline as the drag minimum and exposes a pointer resize separator', () => {
    expect(layoutSource).toContain('const SIDEBAR_MIN_WIDTH = 256')
    expect(layoutSource).toContain('class="sidebar-resize-handle"')
    expect(layoutSource).toContain('role="separator"')
    expect(layoutSource).toContain('@pointerdown="startSidebarResize"')
    expect(layoutSource).toContain('cursor: col-resize;')
    expect(layoutSource).toContain("'--ta-sidebar-layout-width'")
  })

  it('supports keyboard resize and keeps the workspace in the remaining flex area', () => {
    expect(layoutSource).toContain('@keydown="resizeSidebarFromKeyboard"')
    expect(layoutSource).toContain("event.key === 'ArrowLeft'")
    expect(layoutSource).toContain("event.key === 'ArrowRight'")
    expect(layoutSource).toContain('flex: 1;')
    expect(layoutSource).toContain('min-width: 0;')
  })

  it('renders a compact icon rail with contextual tooltips and retains the user action trigger', () => {
    expect(sidebarSource).toContain("collapsed?: boolean")
    expect(sidebarSource).toContain("'toggle-collapse'")
    expect(sidebarSource).toContain('sidebar__compact-navigation')
    expect(sidebarSource).toContain('brand--collapsed')
    expect(sidebarSource).toContain('data-tooltip="展开侧边栏"')
    expect(sidebarSource).toContain('sidebar-user-trigger sidebar-tooltip')
    expect(sidebarSource).toContain('v-if="!collapsed"')
    expect(sidebarSource).toContain('sidebarToggleIcon')
    expect(sidebarSource).toContain('recentIcon')
    expect(sidebarSource).toContain('sidebar-recent-popover')
  })

  it('keeps the task button at its baseline width and preserves compact mode across route-owned layouts', () => {
    expect(sidebarSource).toContain('width: 210px;')
    expect(sidebarSource).toContain('max-width: calc(100% - 28px);')
    expect(sidebarSource).toContain('top: 16px;')
    expect(sidebarSource).toContain('right: 16px;')
    expect(sidebarSource).toContain('style="position: absolute; top: 16px; right: 16px;')
    expect(sidebarSource).toContain('position: absolute !important;')
    expect(layoutSource).toContain('const isSidebarCollapsed = ref(readCollapsedSidebarPreference())')
    expect(layoutSource).toContain('refs deliberately live at module scope')
  })

  it('uses a borderless recent launcher and a viewport-level compact user menu', () => {
    expect(sidebarSource).toContain('.sidebar--collapsed .sidebar-navigation__recent')
    expect(sidebarSource).toContain('border: 0;')
    expect(sidebarSource).toContain('position: fixed;')
    expect(sidebarSource).toContain('left: 62px;')
    expect(sidebarSource).toContain('.sidebar--collapsed .sidebar__footer')
    expect(sidebarSource).toContain('z-index: 20;')
    expect(sidebarSource).toContain('z-index: 21;')
    expect(layoutSource).toContain('window.sessionStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY')
  })

  it('shows the history separator only after its overflowing scroll area is moved past the top edge', () => {
    expect(sidebarSource).toContain('@overflow-change="showHistoryDivider = $event"')
    expect(sidebarSource).toContain('sidebar__header--with-history-divider')
    expect(conversationListSource).toContain('new ResizeObserver(queueOverflowReport)')
    expect(conversationListSource).toContain('element.scrollHeight > element.clientHeight + 1')
    expect(conversationListSource).toContain('element.scrollTop > 1')
    expect(conversationListSource).toContain('@scroll.passive="queueOverflowReport"')
    expect(conversationListSource).toContain('.conversation-list::-webkit-scrollbar')
    expect(conversationListSource).toContain('width: 5px;')
  })

  it('rechecks the separator when conversation content or viewport size changes', () => {
    expect(conversationListSource).toContain('new MutationObserver(queueOverflowReport)')
    expect(conversationListSource).toContain("window.addEventListener('resize', queueOverflowReport)")
    expect(conversationListSource).toContain('if (lastOverflowState === shouldShowDivider) return')
    expect(sidebarSource).not.toMatch(/\.sidebar__header\s*\{[^}]*border-bottom/)
  })
})
