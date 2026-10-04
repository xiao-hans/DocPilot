/**
 * 配额接口（FR-042）：GET 读当前额度与用量，PUT 整体覆盖两个轴。
 * 契约来自 app/contexts/quota/api.py；0 表示「不限」，不是「额度为零」。
 */

import { activeTenantOrThrow, requestJson } from './client'

export interface QuotaView {
  limits: { max_documents: number; max_storage_bytes: number }
  usage: { documents: number; storage_bytes: number }
  remaining: { max_documents: number | null; max_storage_bytes: number | null }
  unlimited: boolean
  updated_at: string | null
}

export function fetchQuota(): Promise<QuotaView> {
  return requestJson<QuotaView>(`/api/v1/tenants/${activeTenantOrThrow()}/quota`)
}

/** PUT 是整体覆盖：只想改一个轴时也要把另一个当前值一并传上去。 */
export function saveQuota(payload: {
  max_documents: number
  max_storage_bytes: number
}): Promise<QuotaView> {
  return requestJson<QuotaView>(`/api/v1/tenants/${activeTenantOrThrow()}/quota`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let size = value / 1024
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[index]}`
}
