import { describe, expect, it } from 'vitest'
import inputSource from './ChatInputBox.vue?raw'
import workspaceSource from './ChatWorkspace.vue?raw'
import pickerSource from '@/components/library/LibraryPickerDialog.vue?raw'

describe('chat Library picker', () => {
  it('adds a Library upload destination below Template Market', () => {
    const marketIndex = inputSource.indexOf('模板市场')
    const libraryIndex = inputSource.indexOf('资料库上传')

    expect(marketIndex).toBeGreaterThan(-1)
    expect(libraryIndex).toBeGreaterThan(marketIndex)
    expect(inputSource).toContain("selectTemplateMenu('library')")
    expect(inputSource).toContain("'open-library-picker': []")
  })

  it('renders a searchable modal that follows the My Templates picker structure', () => {
    expect(pickerSource).toContain('id="library-picker-title"')
    expect(pickerSource).toContain('资料库上传')
    expect(pickerSource).toContain('选择后会作为附件加入当前对话，不会自动发送。')
    expect(pickerSource).toContain('type="search"')
    expect(pickerSource).toContain('library-picker__list')
    expect(pickerSource).toContain("emit('select', item)")
  })

  it('loads active Library documents and stages the chosen item in the current conversation', () => {
    expect(workspaceSource).toContain('@open-library-picker="openLibraryPicker"')
    expect(workspaceSource).toContain('<LibraryPickerDialog')
    expect(workspaceSource).toContain("fetchLibraryItems('file', '', { scope: 'active'")
    expect(workspaceSource).toContain('const { blob, filename } = await fetchLibraryItemBlob(item)')
    expect(workspaceSource).toContain('await fileStore.uploadFile(sourceFile, conversation.id, fileTypeForLibraryItem(item))')
    expect(workspaceSource).toContain('conversationStore.addDraftFile(conversation.id, uploaded)')
    expect(workspaceSource).toContain('资料库文件已加入当前对话，请确认后发送。')
  })
})
