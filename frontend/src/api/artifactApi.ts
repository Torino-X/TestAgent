/**
 * Artifact API - list, detail, and authenticated download operations.
 */

import type { Artifact } from '@/types'
import { apiDownloadBlob, apiGet } from './request'

interface ApiArtifact {
  artifact_id: string
  artifact_type?: Artifact['type']
  file_name: string
  file_size?: number | string
  download_url?: string
  created_at?: string
}

interface ApiPage<T> {
  items: T[]
  total?: number
}

interface ApiArtifactPage {
  artifacts: ApiArtifact[]
  total?: number
}

function formatFileSize(value?: number | string): string {
  if (typeof value === 'string') return value
  if (!value) return ''
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(value / 1024))} KB`
}

function formatDate(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

function mapArtifact(artifact: ApiArtifact): Artifact {
  return {
    id: artifact.artifact_id,
    type: artifact.artifact_type ?? 'test_plan_word',
    name: artifact.file_name,
    size: formatFileSize(artifact.file_size),
    generatedAt: formatDate(artifact.created_at),
    downloadUrl: artifact.download_url
  }
}

/**
 * Download an artifact through the authenticated request layer.
 * The browser receives a Blob and starts a native download through a temporary
 * hidden anchor. No save picker, new window, or direct API navigation.
 */
export async function downloadArtifact(
  artifactId: string,
  fallbackFilename?: string
): Promise<void> {
  const normalizedId = artifactId?.trim()
  if (!normalizedId) {
    throw new Error('产物信息不完整，暂时无法下载，请刷新后重试。')
  }

  const { blob, filename: serverFilename, contentType } = await apiDownloadBlob(
    `/api/artifacts/${encodeURIComponent(normalizedId)}/download`
  )
  const normalizedContentType = (contentType || blob.type || '').toLowerCase()

  // Do not save a successful HTTP response containing an API error envelope.
  if (normalizedContentType.includes('application/json') || normalizedContentType.includes('text/html')) {
    const text = await blob.text().catch(() => '')
    let message = '下载产物失败，请稍后重试。'
    try {
      const parsed = JSON.parse(text) as { message?: string }
      if (parsed.message) message = parsed.message
    } catch {
      // Keep the generic message when the response body is not JSON.
    }
    throw new Error(message)
  }

  if (blob.size === 0) {
    throw new Error('下载的文件为空，请稍后重试。')
  }

  const filename = serverFilename || fallbackFilename || 'download.docx'
  const objectUrl = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = filename
    anchor.style.display = 'none'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
  }
}

export async function fetchTaskArtifacts(taskId: string): Promise<Artifact[]> {
  const page = await apiGet<ApiArtifact[] | ApiPage<ApiArtifact> | ApiArtifactPage>(
    `/api/agent/tasks/${encodeURIComponent(taskId)}/artifacts`
  )
  const artifacts = Array.isArray(page) ? page : ('artifacts' in page ? page.artifacts : page.items)
  return artifacts.map(mapArtifact)
}

export async function fetchArtifactDetail(artifactId: string): Promise<Artifact> {
  const artifact = await apiGet<ApiArtifact>(`/api/artifacts/${encodeURIComponent(artifactId)}`)
  return mapArtifact(artifact)
}
