/**
 * 管理接口：模板、成员与邀请、事件规则（docs/13 §7、docs/11 §6、docs/15 §7）。
 * 与后端 `app/contexts/{templates,automation}/api.py`、`app/api/members.py` 的契约一一对应；
 * 写操作依赖 X-CSRF-Token，读操作对全部成员开放、写操作按角色在后端二次校验。
 */

import { activeTenantOrThrow, requestJson as jsonRequest } from './client'

export type TemplateStatus = 'draft' | 'published' | 'retired'
export type MemberRole = 'owner' | 'admin' | 'member' | 'reviewer'
export type InvitationStatus = 'pending' | 'accepted' | 'expired' | 'revoked'

export interface TemplateField {
  name: string
  type: string
  description?: string
}

export interface TemplateVersionDto {
  id: string
  revision: number
  fields: TemplateField[]
  published_at: string | null
}

export interface TemplateDto {
  id: string
  name: string
  status: TemplateStatus
  draft_fields: TemplateField[]
  latest_revision: number | null
  created_at: string
  updated_at: string
  versions?: TemplateVersionDto[]
}

export interface MemberDto {
  user_id: string
  email: string
  role: MemberRole
  status: string
  joined_at: string
}

export interface InvitationDto {
  id: string
  email: string
  role: MemberRole
  status: InvitationStatus
  created_at: string
  expires_at: string
}

export interface CreatedInvitation {
  invitation_id: string
  email: string
  role: MemberRole
  token: string
  expires_at: string
}

export interface RuleActionDto {
  type: 'tag_document' | 'create_notification'
  params: Record<string, unknown>
}

export interface RuleDto {
  id: string
  name: string
  event_type: string
  condition: Record<string, unknown> | null
  action: RuleActionDto
  status: 'active' | 'paused'
  version: number
  created_by: string | null
  created_at: string
  updated_at: string
  runs?: { applied: number; skipped: number; failed: number }
}

export interface RuleRunDto {
  id: string
  rule_id: string
  rule_name: string
  event_id: string
  event_type: string
  status: 'applied' | 'skipped' | 'failed'
  detail: string | null
  attempt_count: number
  created_at: string
  finished_at: string | null
}

export interface NotificationDto {
  id: string
  rule_id: string
  channel: string
  status: string
  body: string
  object_type: string | null
  object_id: string | null
  created_at: string
}

export interface RuleMeta {
  event_types: string[]
  action_types: Array<RuleActionDto['type']>
  condition_keys: Record<string, string[]>
  max_condition_keys: number
}

function base(): string {
  return `/api/v1/tenants/${activeTenantOrThrow()}`
}

// ---- 模板 ----

export function listTemplates(status?: TemplateStatus): Promise<TemplateDto[]> {
  const query = status ? `?status=${status}` : ''
  return jsonRequest<{ templates: TemplateDto[] }>(`${base()}/templates${query}`).then((r) => r.templates)
}

export function getTemplate(templateId: string): Promise<TemplateDto> {
  return jsonRequest<TemplateDto>(`${base()}/templates/${templateId}`)
}

export function createTemplate(name: string, fields: TemplateField[]): Promise<TemplateDto> {
  return jsonRequest<TemplateDto>(`${base()}/templates`, {
    method: 'POST',
    body: JSON.stringify({ name, fields }),
  })
}

export function updateTemplate(
  templateId: string,
  payload: { name?: string; fields?: TemplateField[] },
): Promise<TemplateDto> {
  return jsonRequest<TemplateDto>(`${base()}/templates/${templateId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function publishTemplate(templateId: string): Promise<TemplateDto & { version?: TemplateVersionDto }> {
  return jsonRequest(`${base()}/templates/${templateId}/publish`, { method: 'POST' })
}

export function retireTemplate(templateId: string): Promise<TemplateDto> {
  return jsonRequest<TemplateDto>(`${base()}/templates/${templateId}/retire`, { method: 'POST' })
}

export function copyTemplate(templateId: string, name?: string): Promise<TemplateDto> {
  return jsonRequest<TemplateDto>(`${base()}/templates/${templateId}/copy`, {
    method: 'POST',
    body: JSON.stringify(name ? { name } : {}),
  })
}

// ---- 成员与邀请 ----

export function listMembers(): Promise<MemberDto[]> {
  return jsonRequest<{ members: MemberDto[] }>(`${base()}/members`).then((r) => r.members)
}

export function changeMemberRole(userId: string, role: MemberRole): Promise<MemberDto> {
  return jsonRequest<MemberDto>(`${base()}/members/${userId}`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  })
}

export function removeMember(userId: string): Promise<{ user_id: string; removed: boolean }> {
  return jsonRequest(`${base()}/members/${userId}`, { method: 'DELETE' })
}

export function listInvitations(): Promise<InvitationDto[]> {
  return jsonRequest<{ invitations: InvitationDto[] }>(`${base()}/invitations`).then(
    (r) => r.invitations,
  )
}

export function inviteMember(email: string, role: MemberRole): Promise<CreatedInvitation> {
  return jsonRequest<CreatedInvitation>(`${base()}/invitations`, {
    method: 'POST',
    body: JSON.stringify({ email, role }),
  })
}

export function revokeInvitation(invitationId: string): Promise<{ invitation_id: string; revoked: boolean }> {
  return jsonRequest(`${base()}/invitations/${invitationId}`, { method: 'DELETE' })
}

// ---- 事件规则 ----

export function listRules(): Promise<{ total: number; rules: RuleDto[] }> {
  return jsonRequest<{ total: number; rules: RuleDto[] }>(`${base()}/automation/rules`)
}

/** 表单选项来自后端白名单；界面不复制清单，避免两边静默漂移。 */
export function getRuleMeta(): Promise<RuleMeta> {
  return jsonRequest<RuleMeta>(`${base()}/automation/meta`)
}

export function createRule(payload: {
  name: string
  event_type: string
  condition: Record<string, unknown> | null
  action: RuleActionDto
}): Promise<RuleDto> {
  return jsonRequest<RuleDto>(`${base()}/automation/rules`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateRule(
  ruleId: string,
  payload: Partial<{
    name: string
    event_type: string
    condition: Record<string, unknown> | null
    action: RuleActionDto
    status: 'active' | 'paused'
  }>,
): Promise<RuleDto> {
  return jsonRequest<RuleDto>(`${base()}/automation/rules/${ruleId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function runRule(ruleId: string): Promise<{ consumed: number; applied: number; skipped: number; failed: number }> {
  return jsonRequest(`${base()}/automation/rules/${ruleId}/run`, { method: 'POST' })
}

export function dispatchEvents(): Promise<{ consumed: number; applied: number; skipped: number; failed: number }> {
  return jsonRequest(`${base()}/automation/dispatch`, { method: 'POST' })
}

export function listRuns(limit = 20): Promise<{ total: number; runs: RuleRunDto[] }> {
  return jsonRequest<{ total: number; runs: RuleRunDto[] }>(`${base()}/automation/runs?limit=${limit}`)
}

export function listNotifications(limit = 20): Promise<{ total: number; notifications: NotificationDto[] }> {
  return jsonRequest<{ total: number; notifications: NotificationDto[] }>(
    `${base()}/automation/notifications?limit=${limit}`,
  )
}
