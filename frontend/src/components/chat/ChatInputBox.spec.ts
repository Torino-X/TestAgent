import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { NMessageProvider } from 'naive-ui'
import ChatInputBox from './ChatInputBox.vue'
import source from './ChatInputBox.vue?raw'
import fileCardSource from '@/components/cards/FileAttachmentCard.vue?raw'
import type { ContextUsageResponse } from '@/types'

function usage(overrides: Partial<ContextUsageResponse> = {}): ContextUsageResponse {
  return {
    conversation_public_id: 'conv_test',
    model: {
      name: 'qwen3.7-max',
      context_window_tokens: 128000,
      window_source: 'model_config'
    },
    usage: {
      used_tokens: 86000,
      available_tokens: 42000,
      percent: 67.2,
      count_mode: 'exact',
      estimated: false,
      over_limit: false,
      unattributed_tokens: 0
    },
    breakdown: {
      conversation_history: 61000,
      project_documents: 12000,
      task_context: 7000,
      user_memory: 4000,
      system_instructions: 2000
    },
    compaction: {
      available: true,
      recommended: false,
      in_progress: false
    },
    available: true,
    snapshot_public_id: 'ctxsnap_123',
    as_of: '2026-08-08T12:00:00Z',
    ...overrides
  }
}

async function renderInput(props: Record<string, unknown> = {}) {
  const app = createSSRApp({
    render: () =>
      h(NMessageProvider, null, {
        default: () => h(ChatInputBox, props)
      })
  })
  return renderToString(app)
}

describe('ChatInputBox context usage footer', () => {
  it('uses the existing grid button as an accessible template and Library menu', () => {
    expect(source).toContain('class="template-quick-menu"')
    expect(source).toContain("emit('open-template-picker')")
    expect(source).toContain("emit('open-template-market')")
    expect(source).toContain("emit('open-library-picker')")
    expect(source).toContain('资料库上传')
    expect(source).toContain('aria-haspopup="menu"')
  })

  it('refines only the composer card height, divider, and outer radius', () => {
    const panelRule = source.match(/\.input-panel\s*\{[^}]+\}/)?.[0] ?? ''
    const wrapRule = source.match(/\.input-panel__textarea-wrap\s*\{[^}]+\}/)?.[0] ?? ''
    const actionsRule = source.match(/\.input-panel__actions\s*\{[^}]+\}/)?.[0] ?? ''

    expect(panelRule).toContain('border-radius: 18px;')
    expect(wrapRule).toContain('min-height: 84px;')
    expect(wrapRule).toContain('padding: 10px 16px;')
    expect(actionsRule).not.toContain('border-top')
    expect(actionsRule).toContain('border-radius: 0 0 18px 18px;')
    expect(source).toContain('.input-panel__tool-icon')
    expect(source).toContain('font-size: 20px;')
  })

  it('renders model name before the context ring as separate footer items', async () => {
    const html = await renderInput({ contextUsage: usage() })

    const uploadIndex = html.indexOf('input-panel__upload-button')
    const templateIndex = html.indexOf('input-panel__template-button')
    const modelIndex = html.indexOf('qwen3.7-max')
    const ringIndex = html.indexOf('context-usage-ring')

    expect(uploadIndex).toBeGreaterThan(-1)
    expect(templateIndex).toBeGreaterThan(uploadIndex)
    expect(modelIndex).toBeGreaterThan(templateIndex)
    expect(ringIndex).toBeGreaterThan(modelIndex)
  })

  it('renders the knowledge base toggle after the template button and before the model name', async () => {
    const html = await renderInput({ contextUsage: usage() })

    const templateIndex = html.indexOf('input-panel__template-button')
    const knowledgeIndex = html.indexOf('input-panel__knowledge-button')
    const modelIndex = html.indexOf('qwen3.7-max')

    expect(templateIndex).toBeGreaterThan(-1)
    expect(knowledgeIndex).toBeGreaterThan(templateIndex)
    expect(modelIndex).toBeGreaterThan(knowledgeIndex)
    expect(html).toContain('data-source-icon="知识库.svg"')
    expect(source).toContain('/src/assets/context-usage/知识库.svg')
  })

  it('defaults the knowledge toggle to AUTO and surfaces the smart-mode tooltip', async () => {
    const html = await renderInput({ contextUsage: usage() })

    expect(html).toContain('aria-pressed="false"')
    expect(html).toContain('知识库：智能模式')
  })

  it('renders the selected strict knowledge mode state', async () => {
    const html = await renderInput({
      contextUsage: usage(),
      knowledgeMode: 'MAAS_STRICT'
    })

    expect(html).toContain('aria-pressed="true"')
    expect(html).toContain('知识库：仅依据公司知识库回答')
    expect(html).toContain('input-panel__knowledge-button--active')
  })

  it('falls back to the current main model name before usage data returns', async () => {
    const html = await renderInput({ modelName: 'deepseek-v4-flash 1M' })

    expect(html).toContain('deepseek-v4-flash 1M')
    expect(html).not.toContain('>上下文<')
  })

  it('uses the approved Stitch input frame and icon button classes', async () => {
    const html = await renderInput({ contextUsage: usage(), draftText: 'hello' })

    expect(html).toContain('input-panel__textarea-wrap')
    expect(html).toContain('input-panel__primary-button')
    expect(html).toContain('input-panel__primary-button--send')
    expect(html).toContain('input-panel__tool-icon')
    expect(html).toContain('aria-label="发送"')
  })

  it('uses the provided progress ring svg asset', async () => {
    const html = await renderInput({ contextUsage: usage() })

    expect(html).toContain('圆环.svg')
    expect(source).toContain('mask: var(--context-ring-mask) center / contain no-repeat')
  })

  it('lets long input grow without covering the action toolbar', () => {
    const wrapRule = source.match(/\.input-panel__textarea-wrap\s*\{[^}]+\}/)?.[0] ?? ''
    expect(wrapRule).toContain('min-height: 84px;')
    expect(wrapRule).not.toContain('\n  height: 84px;')
    expect(source).toContain('max-height: 180px;')
    expect(source).toContain('overflow-y: hidden;')
  })

  it('renders normal percentage and the five popover breakdown rows', async () => {
    const html = await renderInput({ contextUsage: usage(), contextPopoverOpen: true })

    expect(html).toContain('86K / 128K tokens')
    expect(html).toContain('67.2%')
    expect(html).toContain('61K')
    expect(html).toContain('12K')
    expect(html).toContain('7K')
    expect(html).toContain('4K')
    expect(html).toContain('2K')
    expect(html).toContain('70.9%')
    expect(html).toContain('14%')
    expect(html).toContain('8.1%')
    expect(html).toContain('4.7%')
    expect(html).toContain('2.3%')
    for (const label of ['对话历史', '项目资料', '任务上下文', 'Memory', '系统指令']) {
      expect(html).toContain(label)
    }
  })

  it('shows the safe Context Engine receipt and unallocated prompt overhead', async () => {
    const html = await renderInput({
      contextUsage: usage({
        usage: { ...usage().usage, unattributed_tokens: 500 },
        debug_details_enabled: true,
        evidence_receipt: {
          snapshot_public_id: 'ctxsnap_123',
          call_site: 'test_plan.generate.outline',
          profile_key: 'test_plan.generator.v1',
          profile_version: '1',
          included_source_count: 2,
          dropped_source_count: 1,
          included_sources: [
            { kind: 'evidence', source_type: 'task_document_evidence', reference: 'file_requirement_123' },
            { kind: 'conversation', source_type: 'conversation_message', reference: 'msg_456' }
          ]
        }
      }),
      contextPopoverOpen: true
    })

    expect(html).toContain('调用封装与未归类内容')
    expect(html).toContain('500')
    expect(html).toContain('上下文收据')
    expect(html).toContain('test_plan.generate.outline')
    expect(html).toContain('任务证据 · task_document_evidence')
    expect(html).toContain('file_requirement_123')
    expect(html).toContain('已舍弃 1 项')
  })

  it('keeps long multi-call receipts in their own scroll region and exposes one copy action', async () => {
    const receipt = {
      snapshot_public_id: 'ctxsnap_123',
      call_site: 'test_plan.tool_narrative',
      profile_key: 'test_plan.generator.v1',
      profile_version: '1',
      included_source_count: 1,
      dropped_source_count: 0,
      included_sources: [
        { kind: 'memory', source_type: 'user_memory', reference: 'mem_0a882a0746ff4ea08576e3136f432cfe' }
      ]
    }
    const html = await renderInput({
      contextPopoverOpen: true,
      contextUsage: usage({ debug_details_enabled: true, recent_evidence_receipts: [receipt, { ...receipt, snapshot_public_id: 'ctxsnap_456' }] })
    })

    expect(html).toContain('context-usage-popover__receipt-content')
    expect(html).toContain('aria-label="复制全部上下文收据"')
    expect(html).toContain('mem_0a882a0746ff4ea08576e3136f432cfe')
    expect(source).toContain('max-height: min(240px, 32dvh);')
    expect(source).toContain('max-height: calc(100dvh - 64px);')
  })

  it('labels a heuristic fallback instead of presenting it as an assembled context snapshot', async () => {
    const html = await renderInput({
      contextPopoverOpen: true,
      contextUsage: usage({
        available: false,
        snapshot_public_id: null,
        usage: { ...usage().usage, used_tokens: 62, available_tokens: 199938, percent: 0.031, count_mode: 'heuristic', estimated: true },
        breakdown: { conversation_history: 62, project_documents: 0, task_context: 0, user_memory: 0, system_instructions: 0 }
      })
    })

    expect(html).toContain('估算 62 / 128K tokens')
    expect(html).toContain('尚未生成可用的上下文快照；当前仅估算最近对话，未包含附件、任务和系统指令。')
  })

  it('renders the matching svg icon for each context usage breakdown item', async () => {
    const html = await renderInput({ contextUsage: usage(), contextPopoverOpen: true })

    for (const icon of ['对话历史.svg', '项目资料.svg', '任务上下文.svg', 'Memory.svg', '系统指令.svg']) {
      expect(html).toContain(icon)
    }
    expect(html).toContain('context-usage-popover__row-icon')
  })

  it('closes the context usage popover when clicking outside its anchor', () => {
    expect(source).toContain('contextUsageAnchorRef')
    expect(source).toContain("document.addEventListener('pointerdown'")
    expect(source).toContain("document.removeEventListener('pointerdown'")
    expect(source).toContain('!anchor.contains(target)')
    expect(source).toContain('localContextPopoverOpen.value = false')
  })

  it('animates the context ring only while a manual compaction is running', () => {
    const ringClasses = source.match(/const contextRingClasses = computed\([\s\S]*?\n\}\)\nconst contextBreakdownIcons/)?.[0] ?? ''

    expect(ringClasses).toContain("'context-usage-ring--loading': props.contextCompacting")
    expect(ringClasses).not.toContain('props.contextUsageLoading || props.contextCompacting')
  })

  it('renders unknown context window without showing 0%', async () => {
    const html = await renderInput({
      contextPopoverOpen: true,
      contextUsage: usage({
        model: { name: 'qwen3.7-max', context_window_tokens: null, window_source: 'unknown' },
        usage: { ...usage().usage, percent: null }
      })
    })

    expect(html).toContain('上下文窗口未知')
    expect(html).not.toContain('0%')
  })

  it('keeps send/stop, attachment, and template controls present', async () => {
    const sendHtml = await renderInput({ contextUsage: usage() })
    const stopHtml = await renderInput({ contextUsage: usage(), responseActive: true })

    expect(sendHtml).toContain('aria-label="发送"')
    expect(stopHtml).toContain('aria-label="停止任务"')
    expect(sendHtml).toContain('aria-label="上传文件"')
    expect(sendHtml).toContain('aria-label="选择模板"')
  })

  it('does not disable chat controls when usage API is unavailable', async () => {
    const html = await renderInput({
      contextUsageUnavailable: true,
      disabled: false,
      sendDisabled: false,
      draftText: 'hello'
    })

    expect(html).toContain('上下文用量暂不可用')
    expect(html).toContain('aria-label="发送"')
    expect(html).toContain('aria-label="上传文件"')
    expect(html).toContain('aria-label="选择模板"')
    expect(html).not.toContain('<textarea class="input-panel__textarea" disabled')
  })

  it('does not expose or block on Context Engine indexing state', async () => {
    const html = await renderInput({
      draftText: '生成测试用例',
      files: [
        {
          id: 'file_pending',
          name: 'requirements.pdf',
          type: 'requirement_doc',
          size: 1024,
          extension: '.pdf',
          status: 'uploaded'
        }
      ]
    })

    expect(html).toContain('aria-label="发送"')
    expect(html).not.toContain('hourglass_top')
    expect(html).not.toContain('input-panel__pending-index')
    expect(source).not.toContain('pendingIndex')
    expect(source).not.toContain('Context Engine 灌入索引')
  })

  it('wraps uploaded file cards after four items without clipping the row edge', () => {
    expect(source).toContain('grid-template-columns: repeat(4, minmax(0, 1fr));')
    expect(source).toContain('overflow: visible;')
    expect(source).not.toContain('overflow-x: auto;')
    expect(source).not.toContain('scrollbar-width: none;')
  })

  it('keeps confirmed floating file cards at the compact approved height', () => {
    expect(fileCardSource).toContain('.file-card--floating.file-card--confirmed')
    expect(fileCardSource).toContain('height: 110px;')
    expect(fileCardSource).toContain('min-height: 110px;')
  })
})
