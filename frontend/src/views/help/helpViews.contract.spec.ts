import { describe, expect, it } from 'vitest'
import { routes } from '@/router/routes'
import sidebarSource from '@/components/layout/Sidebar.vue?raw'
import homeSource from './HelpHomeView.vue?raw'
import articleSource from './HelpArticleView.vue?raw'
import searchResultsSource from '@/components/help/HelpSearchResults.vue?raw'
import appLayoutSource from '@/components/layout/AppLayout.vue?raw'
import categoryNavSource from '@/components/help/HelpCategoryNav.vue?raw'
import searchBoxSource from '@/components/help/HelpSearchBox.vue?raw'

describe('Help Center route and approved screen contracts', () => {
  it('registers refresh-safe Home and Article routes before the catch-all', () => {
    const paths = routes.map((route) => route.path)
    expect(paths).toContain('/help')
    expect(paths).toContain('/help/article/:slug')
    expect(paths.indexOf('/help')).toBeLessThan(paths.indexOf('/:pathMatch(.*)*'))
  })

  it('keeps authenticated Help available when model configuration is incomplete', () => {
    expect(appLayoutSource).toContain("route.path.startsWith('/help')")
    expect(appLayoutSource).toContain('bypassModelSettingsGate.value')
  })

  it('opens the existing user-menu Help action in a separate browser tab', () => {
    expect(sidebarSource).toContain('<a\n          class="sidebar-user-menu__item"')
    expect(sidebarSource).toContain('href="/help"')
    expect(sidebarSource).toContain('target="_blank"')
    expect(sidebarSource).toContain('rel="noopener"')
    expect(sidebarSource).not.toContain('<RouterLink class="sidebar-user-menu__item" to="/help"')
  })

  it('renders Help as a standalone full-viewport experience', () => {
    expect(homeSource).not.toContain('<AppLayout>')
    expect(homeSource).not.toContain("import AppLayout from '@/components/layout/AppLayout.vue'")
    expect(articleSource).not.toContain('<AppLayout>')
    expect(articleSource).not.toContain("import AppLayout from '@/components/layout/AppLayout.vue'")
    expect(homeSource).toContain('min-height: 100dvh;')
    expect(articleSource).toContain('min-height: 100dvh;')
  })

  it('uses the TestAgent Help Center brand in both Help headers', () => {
    expect(homeSource).toContain('<span>TestAgent帮助中心</span>')
    expect(articleSource).toContain('TestAgent帮助中心')
  })

  it('does not turn the standalone Help tab into a second workspace tab', () => {
    expect(homeSource).not.toContain('返回工作台')
    expect(homeSource).not.toContain('help-home__workspace')
    expect(articleSource).not.toContain('返回工作台')
    expect(articleSource).not.toContain('help-article-page__workspace')
  })

  it('strengthens category headings and removes the misleading visual shortcut glyph', () => {
    expect(categoryNavSource).toMatch(/\.help-category-nav h2\s*{[^}]*color:\s*#24272c;/s)
    expect(categoryNavSource).toMatch(/\.help-category-nav h2\s*{[^}]*font-size:\s*15px;/s)
    expect(categoryNavSource).toMatch(/\.help-category-nav h2\s*{[^}]*font-weight:\s*700;/s)
    expect(categoryNavSource).toMatch(/\.help-category-nav section a\s*{[^}]*color:\s*#5f6670;[^}]*font-size:\s*13px;/s)
    expect(searchBoxSource).not.toContain('<kbd')
    expect(homeSource).toContain("event.key !== '/'")
  })

  it('keeps the approved three equal quick-start cards and search states', () => {
    expect(homeSource).toContain('help-quick-grid')
    expect(homeSource).toContain('featuredArticles.slice(0, 3)')
    expect(homeSource).toContain('HelpSearchResults')
    expect(searchResultsSource).toContain('没有找到相关内容')
  })

  it('provides article navigation, TOC, metadata, and retry states', () => {
    expect(articleSource).toContain('HelpCategoryNav')
    expect(articleSource).toContain('HelpTableOfContents')
    expect(articleSource).toContain('HelpPrevNext')
    expect(articleSource).toContain('readingTimeMinutes')
    expect(articleSource).toContain('重新加载')
  })
})
