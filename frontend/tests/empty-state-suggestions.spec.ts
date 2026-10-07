import { describe, expect, it } from 'vitest'
import emptyStateSource from '../src/components/chat/EmptyState.vue?raw'
import chatInputSource from '../src/components/chat/ChatInputBox.vue?raw'
import chatWorkspaceSource from '../src/components/chat/ChatWorkspace.vue?raw'

describe('empty state suggestion prompts', () => {
  it('uses the requested first two suggestion cards', () => {
    expect(emptyStateSource).toContain("title: '测试方案生成'")
    expect(emptyStateSource).toContain("text: '帮我根据这份PRD需求文档和模板生成测试方案'")
    expect(emptyStateSource).toContain("title: '测试用例生成'")
    expect(emptyStateSource).toContain("text: '根据我提供的文档生成完整的测试用例'")
  })

  it('fills the input without hiding the welcome state when a suggestion is selected', () => {
    expect(emptyStateSource).toContain('@click="selectSuggestion(item.text)"')
    expect(emptyStateSource).toContain("select: [text: string]")
    expect(chatWorkspaceSource).toContain('@select="handleSelectSuggestion"')
    expect(chatWorkspaceSource).toContain('const draftText = ref')
    expect(chatWorkspaceSource).not.toContain('hasSelectedSuggestion')
    expect(chatWorkspaceSource).toContain('showWelcomeState')
    expect(chatWorkspaceSource).toContain(':draft-text="draftText"')
    expect(chatInputSource).toContain('draftText?: string')
    expect(chatInputSource).toContain('watch(')
    expect(chatInputSource).toContain('text.value = value')
  })

  it('keeps an uploaded but unsent new conversation on the welcome state', () => {
    expect(chatWorkspaceSource).toContain("['empty', 'uploaded'].includes(props.conversation.state)")
    expect(chatWorkspaceSource).toContain('props.conversation.messages.length === 0')
    expect(chatWorkspaceSource).toContain('!props.conversation.latestTask')
  })
})
