import { describe, expect, it } from 'vitest'
import chatInputSource from '../src/components/chat/ChatInputBox.vue?raw'
import chatWorkspaceSource from '../src/components/chat/ChatWorkspace.vue?raw'
import fileCardSource from '../src/components/cards/FileAttachmentCard.vue?raw'

describe('chat input file and keyboard interactions', () => {
  it('lays selected files from the left edge above the input panel', () => {
    expect(chatInputSource).toContain('.input-panel__files')
    expect(chatInputSource).toContain('grid-template-columns: repeat(4, minmax(0, 1fr))')
    expect(chatInputSource).toContain('align-items: start')
  })

  it('lets floating file cards emit remove and clears the draft file in the workspace', () => {
    expect(fileCardSource).toContain("remove: [fileId: string]")
    expect(fileCardSource).toContain("@click.stop=\"emit('remove', file.id)\"")
    expect(fileCardSource).toContain('@keydown.stop')
    expect(chatInputSource).toContain('@remove="removeFile"')
    expect(chatWorkspaceSource).toContain('@remove-file="handleRemoveFile"')
  })

  it('sends with Enter while preserving Shift Enter for new lines', () => {
    expect(chatInputSource).toContain('@keydown.enter.exact.prevent="submit"')
    expect(chatInputSource).not.toContain('@keydown.enter.prevent')
  })

  it('routes file paste through the existing parent upload pipeline without blocking text paste', () => {
    expect(chatInputSource).toContain('@paste="handlePaste"')
    expect(chatInputSource).toContain('extractClipboardFiles(event)')
    expect(chatInputSource).toContain("if (!files.length) return")
    expect(chatInputSource).toContain('event.preventDefault()')
    expect(chatInputSource).toContain("emit('paste-files', files)")
    expect(chatWorkspaceSource).toContain('@paste-files="handlePasteFiles"')
    expect(chatWorkspaceSource).toContain("void handleIncomingFiles(files, 'paste')")
  })

  it('keeps textarea styles while wiring autosize behavior to the current min and max height', () => {
    expect(chatInputSource).toContain('ref="textareaRef"')
    expect(chatInputSource).toContain('@input="adjustTextareaHeight"')
    expect(chatInputSource).toContain('@compositionend="adjustTextareaHeight"')
    expect(chatInputSource).toContain('minHeight: 64')
    expect(chatInputSource).toContain('maxHeight: 180')
    expect(chatInputSource).toContain('resizeTextareaToContent(textareaRef.value, textareaSize)')
    expect(chatInputSource).toContain('resetTextareaHeight(textareaRef.value, textareaSize)')
    expect(chatInputSource).toContain('resize: none')
    expect(chatInputSource).toContain('overflow-y: hidden')
  })

  it('keeps the textarea editable when only sending is temporarily blocked', () => {
    expect(chatInputSource).toContain('sendDisabled?: boolean')
    expect(chatInputSource).toContain(':disabled="disabled"')
    // BUG FIX 2026-08-19:submit() 把 disabled/sendDisabled 拆到多行,
    //这里去掉"连续字符串"硬约束,只断言两者都出现且在 submit() 体内。
    expect(chatInputSource).toMatch(/props\.disabled[\s\S]*?props\.sendDisabled/)
    expect(chatWorkspaceSource).toContain(':send-disabled="inputSendDisabled"')
  })

  it('turns the send button into a stop button while an answer is streaming', () => {
    expect(chatInputSource).toContain('responseActive?: boolean')
    expect(chatInputSource).toContain('@click="handlePrimaryAction"')
    expect(chatInputSource).toContain("emit('stop')")
    expect(chatWorkspaceSource).toContain(':response-active="responseActive"')
    expect(chatWorkspaceSource).toContain('@stop="handleStopResponse"')
  })
})
