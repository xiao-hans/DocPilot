/**
 * Webhook 管理接口（FR-041，`docs/21 §6`）。
 *
 * 与后端 `app/contexts/webhooks/api.py` 的八个端点一一对应。两条边界在类型里显式体现：
 * 1. **主密钥未配置时整个能力是关闭的**——`meta.enabled === false` 时界面不给建端点入口，
 *    后端也会返 409。界面不该靠"试试看"来判断这件事。
 * 2. **签名密钥只出现一次**——`create` 与 `rotate` 的响应里有 `secret`，列表与详情里没有。
 *    类型上把 `secret` 标成可选，正是为了逼调用方处理"这次没拿到"的情形。
 */

import { activeTenantOrThrow, requestJson as jsonRequest } from './client'

export type WebhookStatus = 'active' | 'paused'
export type DeliveryStatus = 'pending' | 'inflight' | 'succeeded' | 'failed'

export interface WebhookSigningMeta {
  algorithm: string
  signed_over: string
}

export interface WebhookLimits {
  timeout_seconds: number
  max_attempts: number
  lease_seconds: number
  rotation_grace_seconds: number
}

export interface WebhookMetaDto {
  event_types: string[]
  /** false 表示没配 `WEBHOOK_MASTER_KEY`：不能建端点，投递进程也不会启动。 */
  enabled: boolean
  signing: WebhookSigningMeta
  limits: WebhookLimits
  allow_private_targets: boolean
}

export interface WebhookEndpointDto {
  id: string
  url: string
  url_host: string
  description: string
  event_types: string[]
  status: WebhookStatus
  secret_version: number
  /** 末 6 位指纹，只在创建/轮换的那次响应里出现。 */
  secret_fingerprint: string | null
  grace_seconds: number
  created_at: string
  updated_at: string
  rotated_at: string | null
}

export interface WebhookEndpointListDto {
  total: number
  items: WebhookEndpointDto[]
}

export interface CreatedEndpointDto {
  endpoint: WebhookEndpointDto
  /** 明文密钥，**只在这两次响应里出现**（创建与轮换）。 */
  secret: string
}

export interface WebhookDeliveryDto {
  id: string
  endpoint_id: string
  event_id: string
  event_type: string
  status: DeliveryStatus
  attempts: number
  next_attempt_at: string
  response_status: number | null
  error_kind: string | null
  last_error: string | null
  created_at: string
  completed_at: string | null
}

export interface WebhookDeliveryListDto {
  total: number
  counts: Partial<Record<DeliveryStatus, number>>
  allow_private_targets: boolean
  max_attempts: number
  items: WebhookDeliveryDto[]
}

const base = () => `/api/v1/tenants/${activeTenantOrThrow()}`

export function fetchWebhookMeta(): Promise<WebhookMetaDto> {
  return jsonRequest<WebhookMetaDto>(`${base()}/webhooks/meta`)
}

export function listWebhookEndpoints(): Promise<WebhookEndpointListDto> {
  return jsonRequest<WebhookEndpointListDto>(`${base()}/webhooks`)
}

export function createWebhookEndpoint(payload: {
  url: string
  event_types: string[]
  description?: string
}): Promise<CreatedEndpointDto> {
  return jsonRequest<CreatedEndpointDto>(`${base()}/webhooks`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function setWebhookStatus(
  endpointId: string,
  status: WebhookStatus,
): Promise<WebhookEndpointDto> {
  return jsonRequest<WebhookEndpointDto>(`${base()}/webhooks/${endpointId}/status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  })
}

export function rotateWebhookSecret(
  endpointId: string,
  graceSeconds?: number,
): Promise<CreatedEndpointDto> {
  return jsonRequest<CreatedEndpointDto>(`${base()}/webhooks/${endpointId}/rotate`, {
    method: 'POST',
    body: JSON.stringify(graceSeconds === undefined ? {} : { grace_seconds: graceSeconds }),
  })
}

export function deleteWebhookEndpoint(endpointId: string): Promise<{ deleted: boolean }> {
  return jsonRequest<{ deleted: boolean }>(`${base()}/webhooks/${endpointId}`, {
    method: 'DELETE',
  })
}

export function listWebhookDeliveries(
  status?: DeliveryStatus,
): Promise<WebhookDeliveryListDto> {
  const query = status ? `?status=${status}` : ''
  return jsonRequest<WebhookDeliveryListDto>(`${base()}/webhooks/deliveries${query}`)
}