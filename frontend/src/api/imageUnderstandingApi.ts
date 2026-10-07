import { apiGet, apiPost } from './request'
import type {
  ImageUnderstandingConfig,
  ImageUnderstandingSaveResult,
  ImageUnderstandingTestResult,
  UpdateImageUnderstandingConfigRequest
} from '@/types/imageUnderstanding'

const IMAGE_UNDERSTANDING_API_PREFIX = '/api/image-understanding'

function isMaskedSecret(value: string | undefined): boolean {
  return !value || value.trim() === '' || value.includes('*')
}

function withoutMaskedApiKey(payload: UpdateImageUnderstandingConfigRequest): UpdateImageUnderstandingConfigRequest {
  if (!('api_key' in payload) || !isMaskedSecret(payload.api_key)) return payload
  const nextPayload = { ...payload }
  delete (nextPayload as Partial<UpdateImageUnderstandingConfigRequest>).api_key
  return nextPayload
}

export function getImageUnderstandingConfig(): Promise<ImageUnderstandingConfig> {
  return apiGet<ImageUnderstandingConfig>(`${IMAGE_UNDERSTANDING_API_PREFIX}/config`)
}

export function updateImageUnderstandingConfig(
  payload: UpdateImageUnderstandingConfigRequest
): Promise<ImageUnderstandingSaveResult> {
  return apiPost<ImageUnderstandingSaveResult>(
    `${IMAGE_UNDERSTANDING_API_PREFIX}/config`,
    withoutMaskedApiKey(payload)
  )
}

export function testImageUnderstandingConnection(
  payload?: UpdateImageUnderstandingConfigRequest
): Promise<ImageUnderstandingTestResult> {
  const body: Record<string, unknown> = {}
  if (payload) {
    if (payload.api_base_url) body.api_base_url = payload.api_base_url
    if (payload.model_name) body.model_name = payload.model_name
    if (typeof payload.timeout_seconds === 'number') {
      body.timeout_seconds = payload.timeout_seconds
    }
    if (typeof payload.enable_in_doc_parsing === 'boolean') {
      body.enable_in_doc_parsing = payload.enable_in_doc_parsing
    }
    if (payload.api_key && !isMaskedSecret(payload.api_key)) {
      body.api_key = payload.api_key
    }
  }
  return apiPost<ImageUnderstandingTestResult>(
    `${IMAGE_UNDERSTANDING_API_PREFIX}/config/test`,
    body
  )
}
