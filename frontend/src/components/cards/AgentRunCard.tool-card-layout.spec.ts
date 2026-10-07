import { describe, expect, it } from 'vitest'
import source from './AgentRunCard.vue?raw'

describe('AgentRunCard tool card layout', () => {
  it('keeps the preparation clarification timeline entry as a waiting state, not an inline form', () => {
    expect(source).toContain('正在等待用户确认')
    expect(source).toContain('请在输入框上方完成关键信息选择。')
    expect(source).not.toContain('<PreparationClarificationCard')
  })

  it('keeps an externally displayed section confirmation as a waiting timeline state', () => {
    expect(source).toContain('sectionConfirmationExternal')
    expect(source).toContain('section-confirmation-waiting')
    expect(source).toContain('请在输入框上方确认章节处理策略。')
  })

  it('keeps an externally displayed format-loss confirmation out of the timeline card', () => {
    expect(source).toContain('formatLossExternal')
    expect(source).toContain('format-loss-waiting')
    expect(source).toContain('请确认格式丢失触发方式（建议接受丢失）。')
  })

  it('keeps a compact, expandable receipt after the user confirms a section strategy or supplements information', () => {
    expect(source).toContain("kind: 'confirmation-receipt'")
    expect(source).toContain('已收到用户确认')
    expect(source).toContain('【SUCCESS】')
    expect(source).toContain("message.confirmationReceipt?.kind === 'format_loss'")
    expect(source).toContain('confirmation-receipt-card__details')
    expect(source).toContain('toggleConfirmationReceipt')
    expect(source).toContain('isConfirmationReceiptCollapsed')
    expect(source).toContain('confirmationReceiptHtml')
    expect(source).toContain('<button\n                  type="button"\n                  class="confirmation-receipt-card__header"')
    expect(source).toContain('@click="toggleConfirmationReceipt(item.message.id)"')
    expect(source).not.toContain('@keydown.space.prevent="toggleConfirmationReceipt(item.message.id)"')
    expect(source).toContain('.confirmation-receipt-card__details::before')
    expect(source).toContain('.confirmation-receipt-card__markdown > :deep(ul)')
    expect(source).toContain('.confirmation-receipt-card__markdown > :deep(ul > li)::before')
    expect(source).toContain("content: '✦'")
    expect(source).toContain('color: var(--ta-primary)')
  })

  it('keeps the tool call title in the timeline row above the product card', () => {
    const titleIndex = source.indexOf('<p>{{ item.title }}</p>')
    const cardIndex = source.indexOf('class="tool-runtime-card"')

    expect(titleIndex).toBeGreaterThan(-1)
    expect(cardIndex).toBeGreaterThan(-1)
    expect(titleIndex).toBeLessThan(cardIndex)
  })

  it('renders the product card as a full-width panel, not inside the title flex wrapper', () => {
    expect(source).toContain('v-if="item.kind === \'tool\'"')
    expect(source).toContain('class="tool-runtime-card"')
    expect(source).toMatch(/\.tool-runtime-card\s*\{[\s\S]*width:\s*100%;/)
    expect(source).not.toMatch(/\.tool-runtime-card\s*\{[\s\S]*flex:\s*1 1 100%;/)
  })

  it('makes each tool card expandable from its header and keeps log access compatible with the collapsed state', () => {
    expect(source).toContain('class="tool-runtime-card__header"')
    expect(source).toContain('@click="toggleToolCard(item.message.id)"')
    expect(source).toContain('isToolCardCollapsed(item.message.id) ? ChevronForward : ChevronDown')
    expect(source).toContain('v-if="!isToolCardCollapsed(item.message.id)" class="tool-runtime-card__content"')
    expect(source).toContain('const collapsedToolCardIds = ref(new Set<string>())')
    expect(source).toContain('function toggleToolCard(messageId: string): void')
    expect(source).toContain('expandToolCard(messageId)')
  })

  it('renders tool detail text through the same Markdown renderer used by confirmation receipts', () => {
    expect(source).toContain('class="tool-runtime-card__markdown markdown-body"')
    expect(source).toContain('v-html="toolCardDetailHtml(item.message)"')
    expect(source).toContain('function toolCardDetailHtml(message: ChatMessage): string')
    expect(source).toContain('return renderMarkdown(toolCardPresentation(message).detail)')
    expect(source).toContain('.tool-runtime-card__markdown > :deep(ul > li)::before')
  })

  it('uses the confirmation-card surface for tool, understanding, and execution-plan cards', () => {
    expect(source).toMatch(/\.understanding-result-panel,[\s\S]*?\.execution-plan-panel\s*\{[\s\S]*?background:\s*#ffffff;/)
    expect(source).toMatch(/\.tool-runtime-card\s*\{[\s\S]*?background:\s*#ffffff;/)
    expect(source).toContain('border: 1px solid #dfe3e8;')
  })

  it('keeps understanding and execution-plan details expandable from their headers', () => {
    expect(source).toContain('@click="toggleStepResult(item.id)"')
    expect(source).toContain('isStepResultCollapsed(item.id) ? ChevronForward : ChevronDown')
    expect(source).toContain('v-if="!isStepResultCollapsed(item.id)"')
    expect(source).toContain('const collapsedStepResultIds = ref(new Set<string>())')
    expect(source).toContain('function toggleStepResult(itemId: string): void')
  })

  it('adds a restrained shared elevation to every execution-flow card family', () => {
    const shadow = 'box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 10px rgba(15, 23, 42, 0.035);'
    expect(source).toContain(shadow)
    expect(source).toMatch(/\.confirmation-receipt-card\s*\{[\s\S]*?box-shadow:/)
    expect(source).toMatch(/\.tool-runtime-card\s*\{[\s\S]*?box-shadow:/)
    expect(source).toMatch(/\.understanding-result-panel,[\s\S]*?\.execution-plan-panel\s*\{[\s\S]*?box-shadow:/)
    expect(source).toMatch(/\.section-strategy-node\s*\{[\s\S]*?box-shadow:/)
    expect(source).toMatch(/\.format-loss-banner\s*\{[\s\S]*?box-shadow:/)
  })

  it('uses task-specific understanding text instead of the old test-plan default', () => {
    expect(source).toContain('understanding_summary')
    expect(source).toContain('requirementSummaryMessage')
    expect(source).toContain('requirementSummaryText')
    expect(source).toContain('已理解用户任务')
    expect(source).toContain('根据上传文档')
    expect(source).not.toContain('意图识别')
    expect(source).not.toContain('根据上传文档和用户指令生成测试方案。')
    expect(source).not.toContain('已根据当前输入理解任务目标。')
  })

  it('shows generated document page count instead of business module count in result metrics', () => {
    expect(source).toContain("label: '文档页数'")
    expect(source).toContain('formatPageCount')
  })

  it('does not treat every completed task as a generated Word artifact flow', () => {
    expect(source).toContain('shouldAppendGeneratedArtifactTerminalSteps')
    expect(source).toContain('hasMeaningfulSummaryFacts')
    expect(source).toContain('hasMeaningfulReviewResult')
    expect(source).not.toContain('facts ||')
    expect(source).not.toContain('reviewMessage.value ||')
    expect(source).not.toContain('props.task?.review_result ||')
    expect(source).not.toContain('if (isCompleted.value && !items.some')
    expect(source).not.toContain('|| isCompleted.value)')
  })

  it('defers incremental artifact cards until the task summary is rendered', () => {
    expect(source).toContain('const displayArtifactMessages = computed')
    expect(source).toContain('v-for="message in displayArtifactMessages"')
  })

  it('opens an artifact preview from the card while keeping its download button separate', () => {
    expect(source).toContain('class="artifact-card artifact-card--previewable"')
    expect(source).toContain("@click=\"message.artifact && openArtifactPreview(message.artifact.id)\"")
    expect(source).toContain("@click.stop=\"message.artifact && emit('download-artifact', message.artifact.id, message.artifact.name)\"")
    expect(source).toContain('下载产物')
    expect(source).not.toContain('>\n          预览产物\n')
  })
})
