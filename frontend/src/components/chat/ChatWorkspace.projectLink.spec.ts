import { describe, expect, it } from 'vitest'
import source from './ChatWorkspace.vue?raw'

describe('ChatWorkspace project link', () => {
  it('shows a linked project badge in the chat workspace for project conversations', () => {
    expect(source).toContain('v-if="conversation.projectId && conversation.projectName"')
    expect(source).toContain("name: 'projects-detail'")
    expect(source).toContain("projectIcon from '@/assets/Sidebar-svg/项目.svg'")
    expect(source).toContain('chat-project-link')
    expect(source).toContain("'chat-scroll--with-project'")
    expect(source).toContain('.chat-project-link {')
    expect(source).toContain('border: 0;')
    expect(source).toContain('left: 16px;')
    expect(source).toContain('border-radius: var(--ta-radius-md);')
    expect(source).toContain('.chat-workspace {')
    expect(source).toContain('background: var(--ta-surface);')
  })
})
