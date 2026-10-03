<script setup lang="ts">
/**
 * 团队管理：成员角色与一次性邀请令牌。
 * 角色变更只有 owner 可发起、移除成员 owner/admin 可发起，与后端 `app/api/members.py` 同一套规则；
 * 令牌只在创建时返回一次（后端只存摘要），所以界面把它显式标成"离开后就看不到了"。
 */

import { computed, onMounted, ref } from 'vue'
import {
  changeMemberRole,
  inviteMember,
  listInvitations,
  listMembers,
  removeMember,
  revokeInvitation,
  type InvitationDto,
  type MemberDto,
  type MemberRole,
} from '../api/admin'
import { ApiError } from '../api/client'

const props = defineProps<{ role: string }>()

const members = ref<MemberDto[]>([])
const invitations = ref<InvitationDto[]>([])
const busy = ref(false)
const error = ref('')
const notice = ref('')
const inviteEmail = ref('')
const inviteRole = ref<MemberRole>('member')
const issuedToken = ref('')
const issuedEmail = ref('')

const ROLES: MemberRole[] = ['owner', 'admin', 'member', 'reviewer']
const isOwner = computed(() => props.role === 'owner')
const canManage = computed(() => ['admin', 'owner'].includes(props.role))

const invitationLabels: Record<string, string> = {
  pending: '待接受',
  accepted: '已接受',
  expired: '已过期',
  revoked: '已撤回',
}

async function load() {
  busy.value = true
  error.value = ''
  try {
    const [people, invites] = await Promise.all([listMembers(), listInvitations()])
    members.value = people
    invitations.value = invites
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '团队信息加载失败'
  } finally {
    busy.value = false
  }
}

async function changeRole(member: MemberDto, role: MemberRole) {
  error.value = ''
  notice.value = ''
  try {
    await changeMemberRole(member.user_id, role)
    notice.value = `${member.email} 的角色已更新为 ${role}`
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '角色更新失败'
    await load()
  }
}

async function dropMember(member: MemberDto) {
  error.value = ''
  notice.value = ''
  try {
    await removeMember(member.user_id)
    notice.value = `${member.email} 已移出当前工作空间`
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '移除成员失败'
  }
}

async function sendInvitation() {
  error.value = ''
  notice.value = ''
  const email = inviteEmail.value.trim()
  if (!email) {
    error.value = '请填写邮箱'
    return
  }
  try {
    const created = await inviteMember(email, inviteRole.value)
    issuedToken.value = created.token
    issuedEmail.value = created.email
    inviteEmail.value = ''
    notice.value = `已为 ${created.email} 生成邀请令牌，有效期至 ${created.expires_at.slice(0, 16).replace('T', ' ')}`
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '邀请创建失败'
  }
}

async function revoke(invitation: InvitationDto) {
  error.value = ''
  try {
    await revokeInvitation(invitation.id)
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '撤回邀请失败'
  }
}

function clearToken() {
  issuedToken.value = ''
}

onMounted(load)
</script>

<template>
  <section class="space-y-6" aria-label="团队管理">
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h2 class="font-display text-xl font-semibold text-primary">成员与邀请</h2>
        <p class="mt-1 text-xs text-muted">
          角色决定能做什么；邀请令牌是一次性凭据，当前没有邮件通道，需要人工转交给对方。
        </p>
      </div>
      <button
        class="rounded-control border border-edge px-3 py-2 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
        type="button"
        :disabled="busy"
        @click="load"
      >
        刷新
      </button>
    </div>

    <p v-if="busy" class="text-xs text-muted">加载中…</p>
    <p v-if="error" class="rounded-card border border-danger/40 bg-danger/10 px-4 py-3 text-xs text-danger" role="alert">
      {{ error }}
    </p>
    <p v-if="notice" class="rounded-card border border-edge bg-surface-2 px-4 py-3 text-xs text-secondary" role="status">
      {{ notice }}
    </p>

    <div
      v-if="issuedToken"
      class="space-y-2 rounded-card border border-edge bg-surface px-5 py-4"
      role="region"
      :aria-label="`发给 ${issuedEmail} 的邀请令牌`"
    >
      <p class="text-xs font-semibold text-primary">邀请令牌（只显示这一次）</p>
      <p data-testid="invite-token" class="font-mono text-xs text-secondary break-all">{{ issuedToken }}</p>
      <p class="text-xs text-muted">把令牌交给 {{ issuedEmail }}，对方在登录页“接受邀请”处填入即可加入本工作空间。</p>
      <button
        class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
        type="button"
        @click="clearToken"
      >
        我已转交，关闭
      </button>
    </div>

    <div v-if="canManage" class="space-y-3 rounded-card border border-edge bg-surface px-5 py-4">
      <p class="text-xs font-semibold text-primary">新建邀请</p>
      <div class="flex flex-wrap items-center gap-3">
        <label class="sr-only" for="invite-email">受邀邮箱</label>
        <input
          id="invite-email"
          v-model="inviteEmail"
          class="w-[240px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
          placeholder="name@company.com"
          type="email"
        />
        <label class="sr-only" for="invite-role">分配角色</label>
        <select
          id="invite-role"
          v-model="inviteRole"
          class="rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
        >
          <option v-for="item in ROLES.filter((role) => role !== 'owner')" :key="item" :value="item">
            {{ item }}
          </option>
        </select>
        <button
          class="rounded-control bg-brand px-4 py-2 text-xs font-medium text-on-brand transition-colors hover:bg-brand-soft"
          type="button"
          @click="sendInvitation"
        >
          生成邀请
        </button>
      </div>
    </div>

    <div class="space-y-3">
      <p class="text-xs font-semibold text-primary">成员（{{ members.length }}）</p>
      <ul class="space-y-2">
        <li
          v-for="member in members"
          :key="member.user_id"
          class="flex flex-wrap items-center gap-3 rounded-card border border-edge bg-surface px-5 py-3"
        >
          <span class="text-xs text-primary">{{ member.email }}</span>
          <span class="font-mono text-xs text-muted tabular-nums">{{ member.joined_at.slice(0, 10) }}</span>
          <span class="ml-auto flex items-center gap-2">
            <label class="sr-only" :for="`role-${member.user_id}`">{{ member.email }} 的角色</label>
            <select
              :id="`role-${member.user_id}`"
              class="rounded-control border border-edge bg-surface px-2 py-1.5 text-xs text-secondary disabled:text-muted"
              :value="member.role"
              :disabled="!isOwner"
              @change="changeRole(member, ($event.target as HTMLSelectElement).value as MemberRole)"
            >
              <option v-for="item in ROLES" :key="item" :value="item">{{ item }}</option>
            </select>
            <button
              v-if="canManage"
              class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary transition-colors hover:border-danger hover:text-danger"
              type="button"
              @click="dropMember(member)"
            >
              移出
            </button>
          </span>
        </li>
      </ul>
      <p v-if="!isOwner && members.length" class="text-xs text-muted">只有 owner 可以调整角色。</p>
    </div>

    <div v-if="canManage" class="space-y-3">
      <p class="text-xs font-semibold text-primary">待处理邀请（{{ invitations.length }}）</p>
      <p v-if="!invitations.length" class="text-xs text-muted">当前没有邀请记录。</p>
      <ul class="space-y-2">
        <li
          v-for="invitation in invitations"
          :key="invitation.id"
          class="flex flex-wrap items-center gap-3 rounded-card border border-edge bg-surface px-5 py-3"
        >
          <span class="text-xs text-primary">{{ invitation.email }}</span>
          <span class="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-xs text-secondary">{{ invitation.role }}</span>
          <span class="font-mono text-xs text-muted tabular-nums">
            {{ invitationLabels[invitation.status] ?? invitation.status }} · 有效至
            {{ invitation.expires_at.slice(0, 16).replace('T', ' ') }}
          </span>
          <button
            v-if="invitation.status === 'pending'"
            class="ml-auto rounded-control border border-edge px-3 py-1.5 text-xs text-secondary transition-colors hover:border-danger hover:text-danger"
            type="button"
            @click="revoke(invitation)"
          >
            撤回
          </button>
        </li>
      </ul>
    </div>
  </section>
</template>
