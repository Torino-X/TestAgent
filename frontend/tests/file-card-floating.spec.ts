import { describe, expect, it } from 'vitest'
import chatInputSource from '../src/components/chat/ChatInputBox.vue?raw'
import chatMessageListSource from '../src/components/chat/ChatMessageList.vue?raw'
import fileCardSource from '../src/components/cards/FileAttachmentCard.vue?raw'

describe('floating upload file cards', () => {
  it('uses the floating variant only in the chat input upload area', () => {
    expect(chatInputSource).toContain('variant="floating"')
    expect(chatMessageListSource).not.toContain('variant="floating"')
  })

  it('defines a separate floating card layout without replacing the default card', () => {
    expect(fileCardSource).toContain('variant?:')
    expect(fileCardSource).toContain('file-card--floating')
    expect(fileCardSource).toContain('file-card__status-dot')
    expect(fileCardSource).toContain('file-card__close')
    expect(fileCardSource).toContain('width: 100%')
    expect(fileCardSource).toContain('min-height: 110px')
    expect(fileCardSource).toContain('box-shadow: 0 1px 5px')
  })

  it('removes manual file-role UI from floating upload cards', () => {
    expect(fileCardSource).not.toContain('file-card__type-badge')
    expect(fileCardSource).not.toContain('file-card__confirm-actions')
    expect(fileCardSource).not.toContain('file-card__confirm-button')
    expect(fileCardSource).not.toContain('needsTypeConfirm')
    expect(fileCardSource).not.toContain("confirmType: [fileId: string, fileType: FileType]")
    expect(fileCardSource).not.toContain("'requirement_doc'")
    expect(fileCardSource).not.toContain("'test_plan_template'")
    expect(fileCardSource).not.toContain("'supplemental_doc'")
    expect(chatInputSource).not.toContain('@confirm-type=')
    expect(chatInputSource).not.toContain("'confirm-file-type': [fileId: string, fileType: FileType]")
  })

  it('only exposes upload lifecycle status text on file cards', () => {
    expect(fileCardSource).toContain("uploading: '上传中'")
    expect(fileCardSource).toContain("uploaded: '已上传'")
    expect(fileCardSource).toContain("failed: '上传失败'")
    expect(fileCardSource).not.toContain('正在识别')
    expect(fileCardSource).not.toContain('需求文档')
    expect(fileCardSource).not.toContain('测试方案模板')
    expect(fileCardSource).not.toContain('已确认')
  })

  it('renders a floating upload progress ring while files are uploading', () => {
    expect(fileCardSource).toContain('file-card__upload-progress')
    expect(fileCardSource).toContain('showUploadProgress')
    expect(fileCardSource).toContain('--file-upload-progress')
    expect(fileCardSource).toContain('conic-gradient')
  })
})
