import type { ContextEvidenceReceipt } from '@/types'

const evidenceKindLabels: Record<string, string> = {
  conversation: '对话历史',
  current_goal: '当前目标',
  task_state: '任务状态',
  evidence: '任务证据',
  knowledge: '项目资料',
  memory: 'Memory',
  system_rules: '系统规则',
  project_instructions: '项目指令',
  call_contract: '调用契约'
}

export function contextEvidenceKindLabel(kind: string): string {
  return evidenceKindLabels[kind] ?? kind
}

/** Formats every diagnostic receipt exactly once for copy-to-clipboard. */
export function formatContextEvidenceReceipts(receipts: ContextEvidenceReceipt[]): string {
  if (!receipts.length) return ''

  const calls = receipts.map((receipt, index) => {
    const summary = [
      `调用 ${index + 1}`,
      `调用：${receipt.call_site}`,
      `已纳入 ${receipt.included_source_count} 项来源${receipt.dropped_source_count ? `；已舍弃 ${receipt.dropped_source_count} 项` : ''}`
    ]
    const sources = receipt.included_sources.length
      ? receipt.included_sources.map((source) => {
          const label = `${contextEvidenceKindLabel(source.kind)} · ${source.source_type}`
          return source.reference ? `${label}\n${source.reference}` : label
        })
      : ['无已纳入来源']

    return [...summary, '来源：', ...sources.map((source) => `- ${source}`)].join('\n')
  })

  return [`本任务上下文收据（${receipts.length} 次调用）`, ...calls].join('\n\n')
}
