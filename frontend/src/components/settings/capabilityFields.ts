import type { CapabilityType } from '@/api/settingsApi'

// CE-01: 能力类型列表（SettingsForm 下拉 + 测试共享）
export const CAPABILITY_TYPES: ReadonlyArray<CapabilityType> = [
  'chat',
  'reasoning',
  'embedding',
  'reranker',
  'compression',
  'memory_extraction'
]

// CE-01: 各能力显示的动态字段规则（SettingsForm 与测试共享）
export interface CapabilityFieldRule {
  key: string
  label: string
  type: 'number' | 'text' | 'checkbox'
  applies: (capability: CapabilityType) => boolean
}

export const CAPABILITY_FIELD_RULES: ReadonlyArray<CapabilityFieldRule> = [
  // 通用字段（chat/reasoning 也显示）
  { key: 'api_base_url', label: 'API URL', type: 'text', applies: () => true },
  { key: 'api_key', label: 'API KEY', type: 'text', applies: () => true },
  { key: 'model_name', label: '模型名称', type: 'text', applies: () => true },
  { key: 'timeout_seconds', label: '超时秒数', type: 'number', applies: () => true },
  { key: 'context_window_tokens', label: '上下文窗口 Tokens', type: 'number', applies: () => true },
  // Embedding 专属
  { key: 'embedding_dimension', label: 'Embedding 维度', type: 'number', applies: (c) => c === 'embedding' },
  { key: 'normalize_embeddings', label: '向量归一化', type: 'checkbox', applies: (c) => c === 'embedding' },
  { key: 'max_input_tokens', label: '最大输入 Tokens', type: 'number', applies: (c) => c === 'embedding' },
  { key: 'batch_size', label: '批大小', type: 'number', applies: (c) => c === 'embedding' },
  { key: 'batch_token_limit', label: '批 Token 上限', type: 'number', applies: (c) => c === 'embedding' },
  // Reranker 专属
  { key: 'pre_rerank_limit', label: 'Pre-Rerank Limit', type: 'number', applies: (c) => c === 'reranker' },
  { key: 'max_input_tokens', label: '最大输入 Tokens', type: 'number', applies: (c) => c === 'reranker' },
  { key: 'batch_size', label: '批大小', type: 'number', applies: (c) => c === 'reranker' },
  { key: 'score_type', label: 'Score Type', type: 'text', applies: (c) => c === 'reranker' }
]

export function fieldsForCapability(capability: CapabilityType): CapabilityFieldRule[] {
  return CAPABILITY_FIELD_RULES.filter((rule) => rule.applies(capability))
}

// CE-01: Chat/Reasoning 显示通用字段（不含 embedding/reranker 专属）
export function hasEmbeddingFields(capability: CapabilityType): boolean {
  return fieldsForCapability(capability).some((r) => r.key === 'embedding_dimension')
}

export function hasRerankerFields(capability: CapabilityType): boolean {
  return fieldsForCapability(capability).some((r) => r.key === 'score_type')
}

// CE-01: masked API key 判定（含 * 视为掩码，不覆盖原值）
export function isMaskedApiKey(value: string): boolean {
  const trimmed = (value ?? '').trim()
  return trimmed === '' || trimmed.includes('*')
}
