/**
 * 运营接口：平台用量、平台失败分布，以及本租户用量与失败明细。
 * 与后端 `app/contexts/operations/api.py` 的契约一一对应（DOC-DD-007 §5）。
 *
 * 边界要写清楚，因为它决定界面能显示什么：
 * - 平台两个端点跨租户，后端只输出计数与错误代号，**不含**错误原文、文件名、正文、邮箱；
 *   所以平台区块里没有"点开看某租户的失败原因"这回事，那是后端刻意不给的。
 * - 要看原文只能走租户端点，读的是当前工作空间自己的数据，由成员关系与角色矩阵把关。
 */

import { activeTenantOrThrow, requestJson } from './client'

export interface TenantUsageDto {
  tenant_id: string
  window_days: number
  documents: { total: number; recent_created: number; retention: Record<string, number> }
  file_versions: { total: number; storage_bytes: number }
  jobs: {
    total: number
    by_status: Record<string, number>
    recent_failed: number
    attempts: number
  }
  results: { total: number; by_state: Record<string, number>; human_revisions: number }
  automation: { rules_active: number; runs: Record<string, number>; pending_events: number }
  search_entries: number
}

export interface PlatformTenantRowDto {
  tenant_id: string
  documents: number
  storage_bytes: number
  jobs: Record<string, number>
  jobs_failed_in_window: number
  pending_events: number
  rule_runs_failed: number
}

export interface PlatformUsageDto {
  window_days: number
  total_tenants: number
  limit: number
  offset: number
  totals: { documents: number; storage_bytes: number; pending_events: number }
  tenants: PlatformTenantRowDto[]
}

export interface FailureCodeDto {
  code: string
  label: string
  jobs: number
  tenants_affected: number
  last_seen: string | null
}

export interface PlatformFailuresDto {
  window_days: number
  scanned_rows: number
  truncated: boolean
  scan_cap: number
  tenants_with_failures: number
  tenant_ids: string[]
  codes: FailureCodeDto[]
  generated_at: string
}

export interface FailedJobDto {
  job_id: string
  status: string
  attempt_count: number
  error: string | null
  finished_at: string | null
  code: string
  label: string
}

export interface FailedRuleRunDto {
  run_id: string
  event_type: string
  detail: string | null
  created_at: string
  code: string
  label: string
}

export interface TenantFailuresDto {
  window_days: number
  page: { limit: number; offset: number }
  jobs: { total: number; items: FailedJobDto[] }
  rule_runs: { total: number; items: FailedRuleRunDto[] }
  generated_at: string
}

export function fetchTenantUsage(days: number): Promise<TenantUsageDto> {
  return requestJson<TenantUsageDto>(
    `/api/v1/tenants/${activeTenantOrThrow()}/usage?days=${days}`,
  )
}

export function fetchTenantFailures(days: number, limit = 20): Promise<TenantFailuresDto> {
  return requestJson<TenantFailuresDto>(
    `/api/v1/tenants/${activeTenantOrThrow()}/usage/failures?days=${days}&limit=${limit}`,
  )
}

export function fetchPlatformUsage(days: number, limit = 100): Promise<PlatformUsageDto> {
  return requestJson<PlatformUsageDto>(`/api/v1/platform/usage?days=${days}&limit=${limit}`)
}

export function fetchPlatformFailures(days: number): Promise<PlatformFailuresDto> {
  return requestJson<PlatformFailuresDto>(`/api/v1/platform/usage/failures?days=${days}`)
}

/** 字节数按 KiB/MiB/GiB 口语化；界面不给一长串数字。 */
export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let size = value
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  const digits = index === 0 || size >= 100 ? 0 : 1
  return `${size.toFixed(digits)} ${units[index]}`
}
