import { apiGet, apiPost, apiPut } from './request'

const COMPANY_RAG_API_PREFIX = '/api/knowledge'

export interface CompanyRagConfig {
  config_id: string | null
  enabled: boolean
  api_base_url: string
  api_key_masked: string
  api_key_set: boolean
  timeout_seconds: number
  top_k: number
  similarity_threshold: number
  retrieve_strategy: number
  last_test_status: string | null
  last_test_message: string | null
  updated_at: string | null
}

export interface UpdateCompanyRagConfig {
  enabled: boolean
  api_base_url: string
  api_key?: string
  timeout_seconds: number
  top_k: number
  similarity_threshold: number
  retrieve_strategy: number
}

export interface CompanyRagConnectionTestResult {
  success: boolean
  latency_ms: number
  status: string
  message: string
  error_code?: string | null
  tested_at: string
}

export function getCompanyRagConfig(): Promise<CompanyRagConfig> {
  return apiGet<CompanyRagConfig>(`${COMPANY_RAG_API_PREFIX}/config`)
}

export function updateCompanyRagConfig(payload: UpdateCompanyRagConfig): Promise<CompanyRagConfig> {
  return apiPut<CompanyRagConfig>(`${COMPANY_RAG_API_PREFIX}/config`, payload)
}

export function testCompanyRagConnection(): Promise<CompanyRagConnectionTestResult> {
  return apiPost<CompanyRagConnectionTestResult>(`${COMPANY_RAG_API_PREFIX}/config/test`, {})
}
