/**
 * 工作空间接口：复核队列、结果修订、文档库与检索。
 * 与 docs/14 §5、docs/15 §5 的契约一一对应；写操作依赖 X-CSRF-Token。
 */

import { activeTenantOrThrow, requestJson as jsonRequest } from './client'

export type ResultState = 'machine' | 'revised' | 'confirmed' | 'rejected'
export type ReviewFilter = ResultState | 'all'

export interface ReviewField {
  name: string
  type: string
  description?: string
}

export interface RevisionDto {
  revision_no: number
  source: 'machine' | 'human'
  actor_user_id: string | null
  reason: string | null
  values: Record<string, unknown>
  created_at: string
}

export interface ReviewResult {
  id: string
  job_id: string
  document_version_id: string
  template_version_id: string | null
  filename: string
  doc_type: string | null
  state: ResultState
  revision_no: number
  fields: ReviewField[]
  values: Record<string, unknown>
  warnings: string[]
  confirmed_by: string | null
  confirmed_at: string | null
  created_at: string
  updated_at: string
  revisions?: RevisionDto[]
}

export interface DocumentVersionDto {
  id: string
  revision: number
  original_filename: string
  media_type: string
  size_bytes: number
  checksum: string
  created_at: string
}

export interface DocumentItem {
  id: string
  status: string
  doc_type: string | null
  labels: string[]
  source: string
  retention_status: string
  created_at: string
  updated_at: string | null
  versions: DocumentVersionDto[]
  result_states: string[]
}

export interface SearchHit {
  document_id: string
  document_version_id: string
  kind: 'filename' | 'text' | 'field'
  field_name: string | null
  doc_type: string | null
  snippet: string
}

function base(): string {
  return `/api/v1/tenants/${activeTenantOrThrow()}`
}

export function listResults(params: { state?: ReviewFilter; limit?: number } = {}): Promise<ReviewResult[]> {
  const query = new URLSearchParams()
  if (params.state) query.set('state', params.state)
  query.set('limit', String(params.limit ?? 50))
  return jsonRequest<{ results: ReviewResult[] }>(`${base()}/results?${query}`).then((r) => r.results)
}

export function getResult(resultId: string): Promise<ReviewResult> {
  return jsonRequest<ReviewResult>(`${base()}/results/${resultId}`)
}

export function reviseResult(
  resultId: string,
  values: Record<string, unknown>,
  expectedRevision: number,
  reason?: string,
): Promise<ReviewResult> {
  return jsonRequest<ReviewResult>(`${base()}/results/${resultId}`, {
    method: 'PATCH',
    body: JSON.stringify({ values, expected_revision: expectedRevision, reason: reason ?? null }),
  })
}

export function confirmResult(resultId: string): Promise<ReviewResult> {
  return jsonRequest<ReviewResult>(`${base()}/results/${resultId}/confirm`, { method: 'POST' })
}

export function rejectResult(resultId: string, reason: string): Promise<ReviewResult> {
  return jsonRequest<ReviewResult>(`${base()}/results/${resultId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function versionText(documentVersionId: string): Promise<{ filename: string; char_count: number; text: string }> {
  return jsonRequest(`${base()}/document-versions/${documentVersionId}/text`)
}

export function listDocuments(
  params: { label?: string; docType?: string; retention?: string; q?: string; limit?: number; offset?: number } = {},
): Promise<{ total: number; items: DocumentItem[] }> {
  const query = new URLSearchParams()
  if (params.label) query.set('label', params.label)
  if (params.docType) query.set('doc_type', params.docType)
  if (params.retention) query.set('retention_status', params.retention)
  if (params.q) query.set('q', params.q)
  query.set('limit', String(params.limit ?? 20))
  if (params.offset) query.set('offset', String(params.offset))
  return jsonRequest<{ total: number; items: DocumentItem[] }>(`${base()}/documents?${query}`)
}

export function updateDocumentMetadata(
  documentId: string,
  payload: { labels?: string[]; doc_type?: string; retention_status?: string },
): Promise<Pick<DocumentItem, 'id' | 'labels' | 'doc_type' | 'retention_status'>> {
  return jsonRequest(`${base()}/documents/${documentId}`, { method: 'PATCH', body: JSON.stringify(payload) })
}

export function searchWorkspace(q: string, kind: 'all' | 'filename' | 'text' | 'field' = 'all'): Promise<SearchHit[]> {
  const query = new URLSearchParams({ q, kind })
  return jsonRequest<{ hits: SearchHit[] }>(`${base()}/search?${query}`).then((r) => r.hits)
}

