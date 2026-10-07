import type { PublicExecutionUpdate, ToolCallStatus } from '@/types'

export type ToolDisplayStatus = 'running' | 'retrying' | 'failed' | 'warning' | 'info' | 'success'

/** Resolve the public status without changing the backend tool-call status. */
export function resolveToolDisplayStatus(
  toolStatus?: ToolCallStatus,
  publicUpdate?: Pick<PublicExecutionUpdate, 'level'>,
): ToolDisplayStatus {
  if (publicUpdate?.level === 'retrying') return 'retrying'
  if (toolStatus === 'running') return 'running'
  if (toolStatus === 'failed') return 'failed'
  if (publicUpdate?.level === 'warning') return 'warning'
  if (publicUpdate?.level === 'info') return 'info'
  return 'success'
}

export function toolDisplayStatusLabel(status: ToolDisplayStatus): string {
  switch (status) {
    case 'running': return '执行中'
    case 'retrying': return '正在重试'
    case 'failed': return '执行失败'
    case 'warning': return '需要关注'
    case 'info': return '已处理'
    case 'success': return '已完成'
  }
}
