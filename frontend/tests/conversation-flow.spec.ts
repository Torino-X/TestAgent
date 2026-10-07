import { describe, expect, it } from 'vitest'
import sidebarSource from '../src/components/layout/Sidebar.vue?raw'
import chatWorkspaceSource from '../src/components/chat/ChatWorkspace.vue?raw'
import conversationStoreSource from '../src/stores/conversationStore.ts?raw'

describe('conversation flow wiring', () => {
  it('starts a reusable task conversation from the sidebar', () => {
    expect(conversationStoreSource).toContain('async function startNewConversation')
    expect(conversationStoreSource).toContain('async function createNewConversation')
    expect(sidebarSource).toContain('async function goNewChat')
    expect(sidebarSource).toContain('conversationStore.startNewConversation()')
    expect(sidebarSource).toContain('router.push(`/chat/${target.id}`)')
  })

  it('uses backend-confirmed attachments after the user message is created', () => {
    expect(chatWorkspaceSource).toContain('const attachedFiles = activeConversation.draftFiles.filter')
    expect(chatWorkspaceSource).toContain("!file.localOnly && file.status !== 'failed'")
    expect(chatWorkspaceSource).toContain('const optimisticUserMessage = appendOptimisticUserMessage(text, attachedFiles, clientOrder)')
    expect(chatWorkspaceSource).toContain('onMessageCreated: ({ message, conversation }) =>')
    expect(chatWorkspaceSource).not.toContain('files: attachedFiles')
  })

  it('does not expose the obsolete confirm-file-type request path', () => {
    expect(chatWorkspaceSource).not.toContain('@confirm-file-type="handleConfirmFileType"')
    expect(chatWorkspaceSource).not.toContain('async function handleConfirmFileType')
    expect(chatWorkspaceSource).not.toContain('fileStore.confirmFileType(fileId, fileType)')
    expect(conversationStoreSource).toContain('function updateFile')
  })

  it('sends draft attachments in the original array order', () => {
    const spreadIndex = chatWorkspaceSource.indexOf('const attachedFiles = activeConversation.draftFiles.filter')
    const mapIndex = chatWorkspaceSource.indexOf('const fileIds = attachedFiles.map((file) => file.id)')
    const sendIndex = chatWorkspaceSource.indexOf('messageApi.sendMessageStream(activeConversationId, text, fileIds')

    expect(spreadIndex).toBeGreaterThan(-1)
    expect(mapIndex).toBeGreaterThan(spreadIndex)
    expect(sendIndex).toBeGreaterThan(mapIndex)
    expect(chatWorkspaceSource).not.toContain('attachedFiles.sort')
    expect(chatWorkspaceSource).not.toContain('draftFiles.sort')
  })

  it('funnels picker, paste and drop file imports through one upload function', () => {
    expect(chatWorkspaceSource).toContain('type IncomingFileSource =')
    expect(chatWorkspaceSource).toContain('async function handleIncomingFiles')
    expect(chatWorkspaceSource).toContain("void handleIncomingFiles(files, 'picker')")
    expect(chatWorkspaceSource).toContain("void handleIncomingFiles(files, 'paste')")
    expect(chatWorkspaceSource).toContain("void handleIncomingFiles(files, 'drop')")
    expect(chatWorkspaceSource).toContain('conversationStore.ensureActiveConversation(props.conversation.id)')
    expect(chatWorkspaceSource).toContain('createLocalDraftFile(file, index)')
    expect(chatWorkspaceSource).toContain('conversationStore.addDraftFile(activeConversation.id, localDraft)')
    expect(chatWorkspaceSource).toContain('fileStore.uploadFile(file, activeConversation.id, undefined, {')
    expect(chatWorkspaceSource).toContain('conversationStore.updateDraftFile(activeConversation.id, localDraft.id')
    expect(chatWorkspaceSource).toContain('conversationStore.replaceDraftFile(activeConversation.id, localDraft.id')
    expect(chatWorkspaceSource).toContain('Promise.allSettled(uploads)')
  })

  it('centers the initial welcome state without enabling chat scroll', () => {
    expect(chatWorkspaceSource).toContain(":class=\"{ 'chat-scroll--welcome': showWelcomeState }\"")
    expect(chatWorkspaceSource).toContain('.chat-scroll--welcome')
    expect(chatWorkspaceSource).toContain('align-items: center')
    expect(chatWorkspaceSource).toContain('justify-content: center')
    expect(chatWorkspaceSource).toContain('--welcome-input-safe-area')
    expect(chatWorkspaceSource).toContain('padding-bottom: var(--welcome-input-safe-area)')
    expect(chatWorkspaceSource).toContain('overflow: hidden')
  })

  it('handles file drops only on the chat workspace without adding a visual overlay', () => {
    expect(chatWorkspaceSource).toContain('@dragenter="handleDragEnter"')
    expect(chatWorkspaceSource).toContain('@dragover="handleDragOver"')
    expect(chatWorkspaceSource).toContain('@dragleave="handleDragLeave"')
    expect(chatWorkspaceSource).toContain('@drop="handleDrop"')
    expect(chatWorkspaceSource).toContain('hasFileTransfer(event.dataTransfer)')
    expect(chatWorkspaceSource).toContain("event.dataTransfer.dropEffect = 'copy'")
    expect(chatWorkspaceSource).toContain('isDirectoryTransfer(event.dataTransfer)')
    expect(chatWorkspaceSource).not.toContain('drag-overlay')
    expect(chatWorkspaceSource).not.toContain('drop-zone')
  })
})
