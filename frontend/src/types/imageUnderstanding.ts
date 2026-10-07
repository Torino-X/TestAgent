/** F020 — frontend types for image-understanding configuration. */

export type ImageUnderstandingTestStatus = 'success' | 'failed' | 'unknown'

export interface ImageUnderstandingConfig {
  config_id?: string | null
  api_base_url: string
  api_key_masked?: string
  api_key_set?: boolean
  model_name: string
  timeout_seconds: number
  max_tokens?: number | null
  enable_in_doc_parsing: boolean
  last_test_status?: ImageUnderstandingTestStatus | string | null
  last_test_message?: string | null
  last_test_at?: string | null
  updated_at?: string | null
}

export interface UpdateImageUnderstandingConfigRequest {
  api_base_url: string
  api_key?: string
  model_name: string
  timeout_seconds: number
  max_tokens?: number | null
  enable_in_doc_parsing: boolean
}

export interface ImageUnderstandingSaveResult {
  config_id?: string | null
  saved: boolean
  api_key_set: boolean
  api_key_masked: string
  updated_at: string
}

export interface ImageUnderstandingTestResult {
  success: boolean
  latency_ms: number
  status?: ImageUnderstandingTestStatus | string
  message?: string
  error_code?: string | null
  tested_at: string
}
