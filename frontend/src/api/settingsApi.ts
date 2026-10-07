import type { SettingsConfig } from '@/types'
import { apiGet, apiPatch, apiPost, apiPut } from './request'

export type NarrativeDetailLevel = 'concise' | 'standard' | 'detailed'

export interface NarrativeSettings {
  enabled: boolean
  detail_level: NarrativeDetailLevel
}

interface ApiNarrativeSettings {
  enabled?: boolean
  detail_level?: NarrativeDetailLevel | string
  detail_level_options?: NarrativeDetailLevel[]
}

interface ApiModelSettings {
  api_base_url: string
  api_key_masked?: string
  model_name: string
  timeout_seconds: number
  capability_type?: string
  context_window_tokens?: number | null
  context_window_k?: number | null
  embedding_dimension?: number | null
  normalize_embeddings?: boolean | null
  rerank_instruction?: string | null
  pre_rerank_limit?: number | null
  score_type?: string | null
  enabled?: boolean
  is_default?: boolean
  config_name?: string
  provider?: string
}

// CE-01: 多能力模型配置类型
export type CapabilityType =
  | 'chat'
  | 'reasoning'
  | 'embedding'
  | 'reranker'
  | 'compression'
  | 'memory_extraction'

export interface CapabilityModelConfig {
  capability_type: CapabilityType
  config_name?: string
  provider?: string
  api_base_url: string
  api_key?: string
  model_name: string
  timeout_seconds?: number
  enable_thinking?: boolean
  supports_vision?: boolean
  temperature?: number | null
  max_tokens?: number | null
  context_window_tokens?: number | null
  default_max_output_tokens?: number | null
  embedding_dimension?: number | null
  normalize_embeddings?: boolean | null
  rerank_instruction?: string | null
  pre_rerank_limit?: number | null
  score_type?: string | null
  enabled?: boolean
  is_default?: boolean
  api_key_masked?: string
  configured?: boolean
}

export interface CapabilityListResponse {
  capabilities: Record<string, CapabilityModelConfig[]>
}

export async function fetchCapabilities(): Promise<CapabilityListResponse> {
  return apiGet<CapabilityListResponse>('/api/settings/model/capabilities')
}

export async function fetchCapabilityConfig(
  capabilityType: CapabilityType,
): Promise<Partial<CapabilityModelConfig>> {
  return apiGet<Partial<CapabilityModelConfig>>(
    `/api/settings/model/capability/${capabilityType}`,
  )
}

export async function saveCapabilityConfig(
  capabilityType: CapabilityType,
  config: Partial<CapabilityModelConfig>,
): Promise<Partial<CapabilityModelConfig>> {
  const payload = withOptionalApiKey(
    {
      capability_type: capabilityType,
      api_base_url: config.api_base_url,
      model_name: config.model_name,
      timeout_seconds: config.timeout_seconds ?? 120,
      enable_thinking: config.enable_thinking ?? false,
      supports_vision: config.supports_vision ?? false,
      temperature: config.temperature ?? null,
      max_tokens: config.max_tokens ?? null,
      context_window_tokens: config.context_window_tokens ?? null,
      default_max_output_tokens: config.default_max_output_tokens ?? null,
      embedding_dimension: config.embedding_dimension ?? null,
      normalize_embeddings: config.normalize_embeddings ?? null,
      rerank_instruction: config.rerank_instruction ?? null,
      pre_rerank_limit: config.pre_rerank_limit ?? null,
      score_type: config.score_type ?? null,
      enabled: config.enabled ?? true,
      is_default: config.is_default ?? true,
      config_name: config.config_name,
      provider: config.provider,
    },
    config.api_key ?? '',
  )
  return apiPut<Partial<CapabilityModelConfig>>(
    `/api/settings/model/capability/${capabilityType}`,
    payload,
  )
}

interface ApiKnowledgeSettings {
  api_base_url: string
  api_key_masked?: string
  knowledge_base_id: string
  enabled: boolean
}

interface ApiUploadSettings {
  max_file_size_mb: number
  max_files_per_conversation: number
  allowed_extensions: string[]
}

function isMaskedSecret(value: string): boolean {
  return value.trim() === '' || value.includes('*')
}

function withOptionalApiKey<T extends Record<string, unknown>>(payload: T, apiKey: string): T & { api_key?: string } {
  if (isMaskedSecret(apiKey)) return payload
  return { ...payload, api_key: apiKey }
}

export async function fetchModelSettings(): Promise<Partial<SettingsConfig>> {
  const model = await apiGet<ApiModelSettings>('/api/settings/model')
  return {
    apiBaseUrl: model.api_base_url,
    apiKey: model.api_key_masked ?? '',
    modelName: model.model_name,
    timeoutSeconds: model.timeout_seconds,
    capabilityType: model.capability_type ?? 'chat',
    contextWindowTokens: model.context_window_tokens ?? null,
    contextWindowK: model.context_window_k ?? (
      model.context_window_tokens ? Math.round(model.context_window_tokens / 1000) : null
    ),
    embeddingDimension: model.embedding_dimension ?? null,
    normalizeEmbeddings: model.normalize_embeddings ?? null
  }
}

export async function saveModelSettings(config: SettingsConfig): Promise<SettingsConfig> {
  await apiPut(
    '/api/settings/model',
    withOptionalApiKey({
      config_name: '默认模型配置',
      provider: 'openai-compatible',
      api_base_url: config.apiBaseUrl,
      model_name: config.modelName,
      temperature: 0.2,
      max_tokens: 8192,
      timeout_seconds: config.timeoutSeconds,
      enable_thinking: false,
      capability_type: config.capabilityType ?? 'chat',
      context_window_k: config.contextWindowK ?? null,
      embedding_dimension: config.embeddingDimension ?? null,
      normalize_embeddings: config.normalizeEmbeddings ?? null,
      is_default: true
    }, config.apiKey)
  )
  return config
}

export async function testModelConnection(config?: SettingsConfig): Promise<{ ok: boolean }> {
  const result = await apiPost<{ success: boolean }>(
    '/api/settings/model/test',
    {
      api_base_url: config?.apiBaseUrl ?? '',
      api_key: config?.apiKey ?? '',
      model_name: config?.modelName ?? ''
    }
  )
  return { ok: result.success }
}

export async function fetchKnowledgeBaseSettings(): Promise<Partial<SettingsConfig>> {
  const knowledge = await apiGet<ApiKnowledgeSettings>('/api/settings/knowledge-base')
  return {
    knowledgeBaseUrl: knowledge.api_base_url,
    knowledgeCollection: knowledge.knowledge_base_id,
    enableKnowledgeBase: knowledge.enabled
  }
}

export async function saveKnowledgeBaseSettings(config: SettingsConfig): Promise<SettingsConfig> {
  await apiPut(
    '/api/settings/knowledge-base',
    withOptionalApiKey({
      config_name: '公司知识库',
      api_base_url: config.knowledgeBaseUrl,
      knowledge_base_id: config.knowledgeCollection,
      default_top_k: 5,
      timeout_seconds: 30,
      enabled: config.enableKnowledgeBase
    }, config.apiKey)
  )
  return config
}

export async function testKnowledgeBaseConnection(config?: SettingsConfig): Promise<{ ok: boolean }> {
  const result = await apiPost<{ success: boolean }>(
    '/api/settings/knowledge-base/test',
    {
      api_base_url: config?.knowledgeBaseUrl ?? '',
      api_key: config?.apiKey ?? '',
      knowledge_base_id: config?.knowledgeCollection ?? ''
    }
  )
  return { ok: result.success }
}

export async function fetchUploadSettings(): Promise<Partial<SettingsConfig>> {
  const upload = await apiGet<ApiUploadSettings>('/api/settings/upload')
  return {
    maxFileSizeMb: upload.max_file_size_mb,
    maxFilesPerConversation: upload.max_files_per_conversation,
    allowedExtensions: upload.allowed_extensions
  }
}

export async function saveUploadSettings(config: SettingsConfig): Promise<SettingsConfig> {
  await apiPut(
    '/api/settings/upload',
    {
      max_file_size_mb: config.maxFileSizeMb,
      max_files_per_conversation: config.maxFilesPerConversation,
      allowed_extensions: config.allowedExtensions
    }
  )
  return config
}

// ── Phase 2.9C narrative settings ──────────────────────────────────────


const NARRATIVE_LEVELS: NarrativeDetailLevel[] = ['concise', 'standard', 'detailed']

function normaliseNarrativeLevel(value: unknown): NarrativeDetailLevel {
  if (typeof value === 'string') {
    const normalised = value.trim().toLowerCase()
    if ((NARRATIVE_LEVELS as readonly string[]).includes(normalised)) {
      return normalised as NarrativeDetailLevel
    }
  }
  return 'standard'
}

function normaliseNarrativeSettings(value: ApiNarrativeSettings | undefined): NarrativeSettings {
  return {
    enabled: value?.enabled !== false,
    detail_level: normaliseNarrativeLevel(value?.detail_level)
  }
}

export async function fetchNarrativeSettings(): Promise<NarrativeSettings> {
  const result = await apiGet<ApiNarrativeSettings>('/api/settings/narrative')
  return normaliseNarrativeSettings(result)
}

export async function saveNarrativeSettings(
  enabled: boolean,
): Promise<NarrativeSettings> {
  const payload = { enabled: Boolean(enabled) }
  const result = await apiPatch<ApiNarrativeSettings>(
    '/api/settings/narrative',
    payload,
  )
  return normaliseNarrativeSettings(result)
}

export async function saveAllSettings(config: SettingsConfig): Promise<SettingsConfig> {
  await saveModelSettings(config)
  await saveKnowledgeBaseSettings(config)
  await saveUploadSettings(config)
  return config
}
