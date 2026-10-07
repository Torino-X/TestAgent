import { describe, expect, it } from 'vitest'
import source from './Sidebar.vue?raw'

describe('Sidebar library navigation', () => {
  it('places the library link inside the header, below new task and above the divider', () => {
    const newTaskIndex = source.indexOf('新建任务')
    const libraryIndex = source.indexOf('sidebar-navigation__item--library')
    const compactNavigationIndex = source.indexOf('<div v-if="collapsed" class="sidebar__compact-navigation"')
    const historyIndex = source.indexOf('<ConversationList')

    expect(newTaskIndex).toBeGreaterThan(-1)
    expect(newTaskIndex).toBeLessThan(libraryIndex)
    expect(libraryIndex).toBeGreaterThan(-1)
    expect(libraryIndex).toBeLessThan(compactNavigationIndex)
    expect(libraryIndex).toBeLessThan(historyIndex)
    expect(source).toContain('<span>资料库</span>')
    expect(source).toContain('.sidebar-navigation__item.router-link-active')
  })
})
