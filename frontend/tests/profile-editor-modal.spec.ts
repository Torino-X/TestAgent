import { describe, expect, it } from 'vitest'
import sidebarSource from '../src/components/layout/Sidebar.vue?raw'
import profileEditorModalSource from '../src/components/profile/ProfileEditorModal.vue?raw'

describe('profile editor modal', () => {
  it('opens from the sidebar profile item without adding a route', () => {
    expect(sidebarSource).toContain('ProfileEditorModal')
    expect(sidebarSource).toContain('showProfileEditor')
    expect(sidebarSource).toContain('@click=\"showProfileEditor = true\"')
    expect(sidebarSource).not.toContain('to="/profile"')
  })

  it('matches the Stitch profile editor content', () => {
    expect(profileEditorModalSource).toContain('编辑个人资料')
    expect(profileEditorModalSource).toContain('显示名称')
    expect(profileEditorModalSource).toContain('用户名')
    expect(profileEditorModalSource).toContain('XiaoYun')
    expect(profileEditorModalSource).toContain('xiaoyunyunx')
    expect(profileEditorModalSource).toContain('您的个人资料有助于大家在群聊中认出你。')
    expect(profileEditorModalSource).toContain('取消')
    expect(profileEditorModalSource).toContain('保存')
  })
})
