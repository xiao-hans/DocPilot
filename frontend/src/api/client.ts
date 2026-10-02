/**
 * DocPilot API 客户端：认证会话、工作空间上下文和文档任务接口。
 */

export type FieldType = 'text' | 'number' | 'date'

export interface FieldDefinition {
  name: string
  type: FieldType
  description: string
}

export type JobStatus = 'queued' | 'processing' | 'succeeded' | 'partial' | 'failed'
export type JobProgress = 'queued' | 'parsing' | 'extracting' | 'excel' | 'done'

export interface JobFileInfo {
  name: string
  page_count: number
  chars: number
  ocr_applied: boolean
  error: string | null
}

export interface ResultRow {
  file: string
  values: Record<string, string>
  warnings: string[]
}

export interface JobDto {
  id: string
  status: JobStatus
  progress: JobProgress
  created_at: number
  files: JobFileInfo[]
  fields: FieldDefinition[]
  result: ResultRow[] | null
  warnings: string[]
  error: string | null
  template_version_id?: string | null
  idempotency_key?: string | null
  attempt_count?: number
  finished_at?: number | null
}

export interface CreateJobResponse {
  job_id: string
  status: 'queued'
  reused: boolean
}

export interface ResultDto {
  job_id: string
  status: JobStatus
  progress: JobProgress
  result: ResultRow[] | null
  warnings: string[]
  error: string | null
}

export interface TenantDto {
  id: string
  name: string
  role: string
}

export interface MeDto {
  user: { id: string; email: string; is_platform_admin: boolean }
  tenants: TenantDto[]
}

let csrfToken = ''
let activeTenantId = ''

export function setCsrfToken(value: string) {
  csrfToken = value
}

export function setActiveTenant(id: string) {
  activeTenantId = id
}

/**
 * 默认演示提取字段：发票模板 6 字段。
 * 与后端 app/models/schema.py 的 INVOICE_TEMPLATE 保持一致，作为 API 请求的默认 fields。
 */
export const INVOICE_FIELDS: FieldDefinition[] = [
  { name: '发票号码', type: 'text', description: '发票右上角的发票号码（20 位数字）' },
  { name: '开票日期', type: 'date', description: '发票上的开票日期，格式 YYYY-MM-DD' },
  { name: '销售方名称', type: 'text', description: '销售方（销货单位）的全称' },
  { name: '购买方名称', type: 'text', description: '购买方（购货单位）的全称' },
  {
    name: '价税合计',
    type: 'number',
    description: '发票右下角的价税合计（含税金额），数字，单位元',
  },
  { name: '税额', type: 'number', description: '发票上的合计税额，数字，单位元' },
]

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function extractDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown }
    const detail = body.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail.map((d) => String((d as { msg?: unknown }).msg ?? d)).join('；')
    }
  } catch {
    /* 响应体不是 JSON，走默认文案 */
  }
  return `请求失败（HTTP ${res.status}）`
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    const headers = new Headers(init?.headers)
    const method = (init?.method ?? 'GET').toUpperCase()
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) {
      headers.set('X-CSRF-Token', csrfToken)
    }
    res = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  } catch {
    throw new ApiError(0, '无法连接 DocAI 后端，请确认服务已启动')
  }
  if (!res.ok) throw new ApiError(res.status, await extractDetail(res))
  return (await res.json()) as T
}

export async function fetchMe(): Promise<MeDto> {
  return requestJson<MeDto>('/api/v1/auth/me')
}

export async function refreshCsrf(): Promise<string> {
  const result = await requestJson<{ csrf_token: string }>('/api/v1/auth/csrf')
  csrfToken = result.csrf_token
  return csrfToken
}

export async function login(email: string, password: string): Promise<void> {
  const result = await requestJson<{ csrf_token: string }>('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  csrfToken = result.csrf_token
}

export async function bootstrapPlatform(
  token: string,
  email: string,
  password: string,
  tenantName: string,
): Promise<void> {
  const result = await requestJson<{ csrf_token: string }>('/api/v1/auth/bootstrap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Bootstrap-Token': token },
    body: JSON.stringify({ email, password, tenant_name: tenantName }),
  })
  csrfToken = result.csrf_token
}

export async function createTenant(name: string): Promise<TenantDto> {
  return requestJson<TenantDto>('/api/v1/tenants', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

export async function logout(): Promise<void> {
  await requestJson<{ logged_out: boolean }>('/api/v1/auth/logout', { method: 'POST' })
  csrfToken = ''
  activeTenantId = ''
}

/** POST /api/v1/tenants/{tenant_id}/jobs：上传文件列表 + fields JSON。 */
export function createJob(files: File[], fields: FieldDefinition[]): Promise<CreateJobResponse> {
  if (!activeTenantId) throw new ApiError(400, '请先选择工作空间')
  const form = new FormData()
  for (const file of files) form.append('files', file, file.name)
  form.append('fields', JSON.stringify(fields))
  return requestJson<CreateJobResponse>(`/api/v1/tenants/${activeTenantId}/jobs`, { method: 'POST', body: form })
}

/** GET /api/v1/tenants/{tenant_id}/jobs/{id}：轮询任务状态。 */
export function getJob(jobId: string): Promise<JobDto> {
  if (!activeTenantId) throw new ApiError(400, '请先选择工作空间')
  return requestJson<JobDto>(`/api/v1/tenants/${activeTenantId}/jobs/${jobId}`)
}

/** GET /api/v1/tenants/{tenant_id}/jobs/{id}/result：结构化结果。 */
export function fetchResult(jobId: string): Promise<ResultDto> {
  if (!activeTenantId) throw new ApiError(400, '请先选择工作空间')
  return requestJson<ResultDto>(`/api/v1/tenants/${activeTenantId}/jobs/${jobId}/result`)
}

/** Excel 下载直链：交给浏览器处理 Content-Disposition（禁止 fetch blob）。 */
export function excelDownloadUrl(jobId: string): string {
  if (!activeTenantId) return '#'
  return `/api/v1/tenants/${activeTenantId}/jobs/${jobId}/excel`
}
