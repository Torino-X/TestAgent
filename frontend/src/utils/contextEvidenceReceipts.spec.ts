import { describe, expect, it } from 'vitest'
import { formatContextEvidenceReceipts } from './contextEvidenceReceipts'

describe('context evidence receipt formatter', () => {
  it('copies every completed call and source, including long references', () => {
    const copied = formatContextEvidenceReceipts([
      {
        snapshot_public_id: 'ctxsnap_1',
        call_site: 'test_plan.tool_narrative',
        profile_key: 'test_plan.generator.v1',
        profile_version: '1',
        included_source_count: 2,
        dropped_source_count: 1,
        included_sources: [
          { kind: 'memory', source_type: 'user_memory', reference: 'mem_0a882a0746ff4ea08576e3136f432cfe' },
          { kind: 'evidence', source_type: 'file_summary', reference: 'file_a63cab63' }
        ]
      },
      {
        snapshot_public_id: 'ctxsnap_2',
        call_site: 'chat.reply',
        profile_key: 'chat.reply.v1',
        profile_version: '1',
        included_source_count: 1,
        dropped_source_count: 0,
        included_sources: [{ kind: 'conversation', source_type: 'conversation_message', reference: 'msg_524011cf' }]
      }
    ])

    expect(copied).toContain('本任务上下文收据（2 次调用）')
    expect(copied).toContain('调用：test_plan.tool_narrative')
    expect(copied).toContain('调用：chat.reply')
    expect(copied).toContain('Memory · user_memory')
    expect(copied).toContain('mem_0a882a0746ff4ea08576e3136f432cfe')
    expect(copied).toContain('任务证据 · file_summary')
    expect(copied).toContain('file_a63cab63')
    expect(copied).toContain('对话历史 · conversation_message')
    expect(copied).toContain('msg_524011cf')
    expect(copied).toContain('已舍弃 1 项')
  })

  it('returns empty content when receipts are unavailable', () => {
    expect(formatContextEvidenceReceipts([])).toBe('')
  })
})
