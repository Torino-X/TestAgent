import { describe, expect, it } from 'vitest'
import source from './ChatInputBox.vue?raw'
import workspaceSource from './ChatWorkspace.vue?raw'

describe('ChatInputBox runtime button state', () => {
  it('reduces the empty composer card height by 20% while retaining the existing resize ceiling', () => {
    expect(source).toContain('rows="1"')
    expect(source).toContain('minHeight: 27')
    expect(source).toMatch(/\.input-panel__textarea-wrap\s*\{[\s\S]*min-height:\s*62px;/)
    expect(source).toMatch(/\.input-panel__textarea\s*\{[\s\S]*min-height:\s*27px;/)
    expect(source).toMatch(/\.input-panel__textarea\s*\{[\s\S]*max-height:\s*180px;/)
    expect(source).toMatch(/\.input-panel__actions\s*\{[\s\S]*min-height:\s*40px;/)
  })

  it('matches the floating confirmation cards at the shared 780px interaction width', () => {
    expect(source).toMatch(/\.input-stack\s*\{[\s\S]*width:\s*min\(100%, 780px\);/)
  })

  it('reports the visible composer panel offset so floating interaction cards keep a stable gap', () => {
    expect(source).toContain('ref="dockRef"')
    expect(source).toContain('ref="panelRef"')
    expect(source).toContain("'dock-layout': [layout: { panelTopOffset: number }]")
    expect(source).toContain('const panelTopOffset = Math.ceil(dockRect.bottom - panelRect.top)')
    expect(source).toContain("emit('dock-layout', { panelTopOffset })")
  })

  it('keeps composer-owned operation popovers above floating confirmation cards', () => {
    expect(source).toMatch(/\.input-dock\s*\{[\s\S]*z-index:\s*20;/)
  })

  it('uses an empty active composer as stop and a typed active composer as send', () => {
    expect(workspaceSource).toContain(':runtime-active="inputRuntimeActive"')
    expect(workspaceSource).toContain('taskBlocksInput(props.conversation)')
    expect(workspaceSource).toContain('responseActive.value || taskBlocksInput(props.conversation)')

    expect(source).toContain('runtimeActive?: boolean')
    expect(source).toContain('runtimeActive: false')
    expect(source).toContain('const runningState = computed(() => props.responseActive || props.runtimeActive)')
    expect(source).toContain('const hasDraftText = computed(() => !!text.value.trim())')
    expect(source).toContain('const primaryActionIsSend = computed(() => !runningState.value || hasDraftText.value)')
    expect(source).toContain('primaryButtonClasses')
    expect(source).toContain("'input-panel__primary-button--send': primaryActionIsSend.value")
    expect(source).toContain("'input-panel__primary-button--stop': !primaryActionIsSend.value")
    expect(source).toContain(':component="primaryActionIsSend ? ArrowUpOutline : Stop"')
    expect(source).toContain("if (primaryActionIsSend.value) return '发送'")
    expect(source).toContain("emit('stop')")
    expect(source).toContain('background: #191c1f;')
    expect(source).toContain('opacity: 1;')
  })

  it('allows submit during runtime so the workspace can cancel then replace the old work', () => {
    const submitStart = source.indexOf('function submit()')
    const actionStart = source.indexOf('function handlePrimaryAction()', submitStart)
    const submitSource = source.slice(submitStart, actionStart)

    expect(submitSource).not.toContain('runningState.value')
    expect(source).toContain('if (!primaryActionIsSend.value)')
    expect(source).toContain("emit('stop')")
    expect(source).toContain('submit()')
  })

  it('keeps model name and context usage next to the send button', () => {
    const actionsStart = source.indexOf('<div class="input-panel__actions">')
    const toolsStart = source.indexOf('<div class="input-panel__tools">', actionsStart)
    const rightStart = source.indexOf('<div class="input-panel__right">', actionsStart)
    const modelStart = source.indexOf('class="input-panel__model-name"', rightStart)
    const contextStart = source.indexOf('class="context-usage-anchor"', rightStart)
    const primaryStart = source.indexOf('class="input-panel__primary-button"', rightStart)

    expect(actionsStart).toBeGreaterThan(-1)
    expect(toolsStart).toBeGreaterThan(actionsStart)
    expect(rightStart).toBeGreaterThan(toolsStart)
    expect(modelStart).toBeGreaterThan(rightStart)
    expect(contextStart).toBeGreaterThan(modelStart)
    expect(primaryStart).toBeGreaterThan(contextStart)
  })

  it('renders the context usage ring as a visible gray icon when usage is unknown', () => {
    expect(source).toContain('conic-gradient(#3d78f6 var(--context-ring-progress), #d1d5db 0)')
    expect(source).toContain('.context-usage-ring--unknown')
    expect(source).toContain('background: #d1d5db;')
    expect(source).toContain('.context-usage-ring--unavailable')
  })

  it('does not render context receipts unless the server enables debug details', () => {
    expect(source).toContain('if (!props.contextUsage?.debug_details_enabled) return []')
    expect(source).toContain('contextEvidenceReceipts.length')
    expect(source).toContain('copyContextEvidenceReceipts')
    expect(source).toContain('context-usage-popover__receipt-content')
  })
})
