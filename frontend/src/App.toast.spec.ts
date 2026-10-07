import { describe, expect, it } from 'vitest'
import appSource from './App.vue?raw'
import workspaceSource from './components/chat/ChatWorkspace.vue?raw'

describe('TestAgent toast system', () => {
  it('centralizes a branded neutral message surface at the application provider', () => {
    expect(appSource).toContain('container-class="testagent-toast-container"')
    expect(appSource).toContain('placement="top"')
    expect(appSource).toContain('Message: {')
    expect(appSource).toContain("borderRadius: '14px'")
    expect(appSource).toContain("boxShadow: '0 16px 40px rgba(24, 24, 27, 0.14), inset 0 0 0 1px rgba(24, 24, 27, 0.08)'")
  })

  it('thanks the user after feedback is persisted', () => {
    expect(workspaceSource).toMatch(/setMessageFeedback\(message\.id, feedback\)[\s\S]*toast\.success\('感谢反馈'\)/)
  })

  it('sizes each toast to its content and centers the icon-text group', () => {
    const messageRule = appSource.match(/\.testagent-toast-container \.n-message\s*\{[^}]+\}/)?.[0] ?? ''
    const contentRule = appSource.match(/\.testagent-toast-container \.n-message__content\s*\{[^}]+\}/)?.[0] ?? ''

    expect(messageRule).toContain('width: fit-content;')
    expect(messageRule).toContain('min-width: 0;')
    expect(messageRule).toContain('justify-content: center;')
    expect(messageRule).not.toContain('min-width: 180px;')
    expect(contentRule).toContain('text-align: center;')
  })

  it('confirms both knowledge-answer mode changes only after persistence succeeds', () => {
    expect(workspaceSource).toMatch(
      /const confirmedMode = await conversationStore\.toggleKnowledgeMode\(activeConversation\.id\)[\s\S]*toast\.success\([\s\S]*confirmedMode === 'MAAS_STRICT'[\s\S]*'已开启知识库回答'[\s\S]*'已关闭知识库回答'/
    )
  })
})
