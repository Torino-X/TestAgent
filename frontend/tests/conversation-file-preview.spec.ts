import { describe, expect, it } from 'vitest'
import fileCardSource from '../src/components/cards/FileAttachmentCard.vue?raw'
import chatInputSource from '../src/components/chat/ChatInputBox.vue?raw'
import chatMessageListSource from '../src/components/chat/ChatMessageList.vue?raw'
import chatWorkspaceSource from '../src/components/chat/ChatWorkspace.vue?raw'

describe('conversation file-card preview wiring', () => {
  it('makes only previewable cards pointer and keyboard accessible', () => {
    expect(fileCardSource).toContain('canPreviewFileAttachment(props.file)')
    expect(fileCardSource).toContain(":role=\"canPreview ? 'button' : undefined\"")
    expect(fileCardSource).toContain(":tabindex=\"canPreview ? 0 : undefined\"")
    expect(fileCardSource).toContain('@keydown.enter.prevent="openPreview"')
    expect(fileCardSource).toContain('@keydown.space.prevent="openPreview"')
    expect(fileCardSource).toContain('open: [file: FileAttachment]')
  })

  it('keeps the floating-card remove control isolated from preview activation', () => {
    expect(fileCardSource).toContain('@click.stop="emit(\'remove\', file.id)"')
    expect(fileCardSource).toContain('@keydown.stop')
  })

  it('forwards open requests from both the input and sent-message surfaces', () => {
    expect(chatInputSource).toContain('@open="openFile"')
    expect(chatInputSource).toContain("'open-file': [file: FileAttachment]")
    expect(chatMessageListSource).toContain('@open="(file) => emit(\'open-file\', file)"')
    expect(chatMessageListSource).toContain("'open-file': [file: FileAttachment]")
  })

  it('routes both surfaces to the shared chat-aware document preview', () => {
    expect(chatWorkspaceSource.match(/@open-file="handleOpenFile"/g)).toHaveLength(2)
    expect(chatWorkspaceSource).toContain('buildChatFilePreviewLocation')
    expect(chatWorkspaceSource).toContain('router.push(buildChatFilePreviewLocation(')
  })
})
