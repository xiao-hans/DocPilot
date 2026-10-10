<script setup lang="ts">
/**
 * Webhook 管理（FR-041，维护轮 M14）。
 *
 * 两条边界在界面上要看得见，不能靠用户试出来：
 * 1. **主密钥未配置时能力是关闭的**：`meta.enabled === false` 时不显示建端点表单，
 *    而是显式说明"机制已交付但默认关闭"，并给出配置项名字。后端也会返 409，
 *    但让用户点一次再看报错是把设计决定藏起来。
 * 2. **签名密钥只出现一次**：创建与轮换之后把明文摊在界面上、只此一次，并说明离开就取不回。
 *    列表里只有末 6 位指纹。
 *
 * 权限：读列表与投递明细对 owner/admin/member 开放（member 排障需要看到失败），
 * `reviewer` 拿不到这个页签；写操作只 owner/admin。与后端 `READ_ROLES`/`WRITE_ROLES` 一致。
 */

import { computed, onMounted, ref } from 'vue'
import { ApiError } from '../api/client'
import {
  createWebhookEndpoint,
  deleteWebhookEndpoint,
  fetchWebhookMeta,
  listWebhookDeliveries,
  listWebhookEndpoints,
  rotateWebhookSecret,
  setWebhookStatus,
  type DeliveryStatus,
  type WebhookDeliveryDto,
  type WebhookEndpointDto,
  type WebhookMetaDto,
} from '../api/webhooks'

const props = defineProps<{ role: string }>()

const meta = ref<WebhookMetaDto | null>(null)
const endpoints = ref<WebhookEndpointDto[]>([])
const deliveries = ref<WebhookDeliveryDto[]>([])
const deliveryCounts = ref<Partial<Record<DeliveryStatus, number>>>({})
const deliveryFilter = ref<'' | DeliveryStatus>('')
const busy = ref(false)
const error = ref('')
const notice = ref('')

/** 明文密钥只在这一处出现，且只在这两次操作之后；离开界面即不可取回。 */
const shownSecret = ref<{ secret: string; fingerprint: string; endpointId: string } | null>(null)

const form = ref({ url: '', description: '', event_types: [] as string[] })

const canManage = computed(() => ['admin', 'owner'].includes(props.role))
const enabled = computed(() => meta.value?.enabled === true)

const statusLabels: Record<string, string> = { active: '启用', paused: '已暂停' }
const deliveryLabels: Record<string, string> = {
  pending: '待投递',
  inflight: '投递中',
  succeeded: '成功',
  failed: '终态失败',
}

async function load() {
  busy.value = true
  error.value = ''
  try {
    meta.value = await fetchWebhookMeta()
    const listed = await listWebhookEndpoints()
    endpoints.value = listed.items
    const feed = await listWebhookDeliveries(deliveryFilter.value || undefined)
    deliveries.value = feed.items
    deliveryCounts.value = feed.counts
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : 'Webhook 数据加载失败'
  } finally {
    busy.value = false
  }
}

function toggleEventType(value: string) {
  const current = form.value.event_types
  form.value.event_types = current.includes(value)
    ? current.filter((item) => item !== value)
    : [...current, value]
}

async function submit() {
  error.value = ''
  notice.value = ''
  shownSecret.value = null
  if (!form.value.url.trim()) {
    error.value = '请填写回调地址'
    return
  }
  if (form.value.event_types.length === 0) {
    error.value = '至少订阅一个事件'
    return
  }
  busy.value = true
  try {
    const created = await createWebhookEndpoint({
      url: form.value.url.trim(),
      event_types: form.value.event_types,
      description: form.value.description.trim(),
    })
    shownSecret.value = {
      secret: created.secret,
      fingerprint: created.endpoint.secret_fingerprint ?? '',
      endpointId: created.endpoint.id,
    }
    form.value = { url: '', description: '', event_types: [] }
    notice.value = '端点已创建'
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '端点创建失败'
  } finally {
    busy.value = false
  }
}

async function toggleStatus(endpoint: WebhookEndpointDto) {
  error.value = ''
  notice.value = ''
  busy.value = true
  try {
    const next = endpoint.status === 'active' ? 'paused' : 'active'
    await setWebhookStatus(endpoint.id, next)
    notice.value = next === 'active' ? '端点已启用' : '端点已暂停，不再投递'
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '状态更新失败'
  } finally {
    busy.value = false
  }
}

async function rotate(endpoint: WebhookEndpointDto) {
  error.value = ''
  notice.value = ''
  shownSecret.value = null
  busy.value = true
  try {
    const rotated = await rotateWebhookSecret(endpoint.id)
    shownSecret.value = {
      secret: rotated.secret,
      fingerprint: rotated.endpoint.secret_fingerprint ?? '',
      endpointId: rotated.endpoint.id,
    }
    notice.value = `密钥已轮换到 v${rotated.endpoint.secret_version}，旧密钥在宽限窗口内仍可验签`
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '密钥轮换失败'
  } finally {
    busy.value = false
  }
}

async function remove(endpoint: WebhookEndpointDto) {
  error.value = ''
  notice.value = ''
  busy.value = true
  try {
    await deleteWebhookEndpoint(endpoint.id)
    notice.value = '端点已删除'
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '端点删除失败'
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="space-y-6">
    <header>
      <h2 class="text-2xl font-semibold text-primary">Webhook</h2>
      <p class="mt-1 text-sm text-secondary">
        出站事件投递：平台把租户内的事件按订阅推给你的回调地址，签名算法与上限由后端单点提供。
      </p>
    </header>

    <p v-if="error" class="rounded-card border border-danger/40 bg-danger/10 px-4 py-3 text-xs text-danger" role="alert">
      {{ error }}
    </p>
    <p v-if="notice" class="rounded-card border border-edge bg-surface-2 px-4 py-3 text-xs text-secondary" role="status">
      {{ notice }}
    </p>

    <!-- 能力关闭：把"为什么看不到表单"说清楚，而不是让用户点一次再读 409 -->
    <div
      v-if="meta && !enabled"
      class="rounded-card border border-edge bg-surface-2 px-5 py-4"
      data-testid="webhook-disabled"
    >
      <p class="text-sm font-semibold text-primary">出站投递当前关闭</p>
      <p class="mt-1 text-xs text-secondary">
        机制已交付但默认关闭：没有配置 <code>WEBHOOK_MASTER_KEY</code> 时不允许登记端点，投递进程也不会启动。
        这是刻意的最小权限姿态——未确认部署侧密钥流程之前，不应打开一个真实的外呼通道。
      </p>
    </div>

    <div v-else-if="meta" class="rounded-card border border-edge bg-surface px-5 py-4 text-xs text-secondary">
      <p>
        签名：<code>{{ meta.signing.algorithm }}</code>，覆盖 <code>{{ meta.signing.signed_over }}</code>
        · 超时 {{ meta.limits.timeout_seconds }} 秒 · 最多 {{ meta.limits.max_attempts }} 次
        · 轮换宽限 {{ meta.limits.rotation_grace_seconds }} 秒
      </p>
      <p class="mt-1">
        目标策略：{{ meta.allow_private_targets ? '已放行内网与回环目标（仅本机联调）' : '只允许 https，且解析到回环/内网即拒绝' }}
      </p>
    </div>

    <!-- 密钥只此一次 -->
    <div
      v-if="shownSecret"
      class="rounded-card border border-brand/40 bg-brand/5 px-5 py-4"
      data-testid="webhook-secret"
    >
      <p class="text-sm font-semibold text-primary">签名密钥（只显示这一次）</p>
      <p class="mt-2 break-all font-mono text-xs text-primary">{{ shownSecret.secret }}</p>
      <p class="mt-2 text-xs text-secondary">
        指纹 {{ shownSecret.fingerprint }}。离开本页后列表里只保留末 6 位，完整密钥取不回——
        请现在复制到你自己的密钥管理里。
      </p>
    </div>

    <div v-if="canManage && enabled" class="space-y-3 rounded-card border border-edge bg-surface px-5 py-4">
      <p class="text-xs font-semibold text-primary">登记端点</p>
      <div class="flex flex-wrap items-center gap-2">
        <input
          v-model="form.url"
          class="w-[320px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
          placeholder="https://example.com/hooks/docpilot"
          type="text"
          aria-label="回调地址"
        />
        <input
          v-model="form.description"
          class="w-[180px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
          placeholder="备注（可选）"
          type="text"
          aria-label="备注"
        />
        <button
          class="rounded-control bg-brand px-4 py-2 text-xs font-medium text-on-brand transition-colors hover:bg-brand-soft"
          type="button"
          data-testid="create-webhook"
          :disabled="busy"
          @click="submit()"
        >
          登记端点
        </button>
      </div>
      <fieldset class="flex flex-wrap items-center gap-3">
        <legend class="text-xs text-muted">订阅事件</legend>
        <label
          v-for="item in meta?.event_types ?? []"
          :key="item"
          class="flex items-center gap-1 text-xs text-secondary"
        >
          <input
            type="checkbox"
            :checked="form.event_types.includes(item)"
            :value="item"
            @change="toggleEventType(item)"
          />
          {{ item }}
        </label>
      </fieldset>
    </div>

    <div class="space-y-3">
      <p class="text-xs font-semibold text-primary">已登记端点（{{ endpoints.length }}）</p>
      <p v-if="endpoints.length === 0" class="text-xs text-muted" data-testid="webhook-empty">
        还没有登记任何端点。
      </p>
      <table v-else class="w-full text-left text-xs">
        <thead class="text-muted">
          <tr>
            <th class="py-2">主机</th>
            <th class="py-2">订阅</th>
            <th class="py-2">状态</th>
            <th class="py-2">密钥版本</th>
            <th v-if="canManage" class="py-2">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="endpoint in endpoints" :key="endpoint.id" class="border-t border-edge">
            <td class="py-2">
              <span class="text-primary">{{ endpoint.url_host }}</span>
              <span v-if="endpoint.description" class="ml-2 text-muted">{{ endpoint.description }}</span>
            </td>
            <td class="py-2 text-secondary">{{ endpoint.event_types.join('、') }}</td>
            <td class="py-2 text-secondary">{{ statusLabels[endpoint.status] ?? endpoint.status }}</td>
            <td class="py-2 text-secondary">v{{ endpoint.secret_version }}</td>
            <td v-if="canManage" class="space-x-2 py-2">
              <button class="text-brand hover:underline" type="button" :disabled="busy" @click="toggleStatus(endpoint)">
                {{ endpoint.status === 'active' ? '暂停' : '启用' }}
              </button>
              <button class="text-brand hover:underline" type="button" :disabled="busy" @click="rotate(endpoint)">
                轮换密钥
              </button>
              <button class="text-danger hover:underline" type="button" :disabled="busy" @click="remove(endpoint)">
                删除
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="space-y-3">
      <div class="flex flex-wrap items-center gap-3">
        <p class="text-xs font-semibold text-primary">投递明细</p>
        <select
          v-model="deliveryFilter"
          class="rounded-control border border-edge bg-surface px-2 py-1 text-xs text-primary"
          aria-label="投递状态筛选"
          @change="load()"
        >
          <option value="">全部状态</option>
          <option v-for="(label, key) in deliveryLabels" :key="key" :value="key">{{ label }}</option>
        </select>
        <span class="text-xs text-muted">
          待投递 {{ deliveryCounts.pending ?? 0 }} · 投递中 {{ deliveryCounts.inflight ?? 0 }}
          · 成功 {{ deliveryCounts.succeeded ?? 0 }} · 终态失败 {{ deliveryCounts.failed ?? 0 }}
        </span>
      </div>
      <p v-if="deliveries.length === 0" class="text-xs text-muted" data-testid="delivery-empty">
        没有符合条件的投递记录。
      </p>
      <table v-else class="w-full text-left text-xs">
        <thead class="text-muted">
          <tr>
            <th class="py-2">事件</th>
            <th class="py-2">状态</th>
            <th class="py-2">尝试</th>
            <th class="py-2">响应</th>
            <th class="py-2">失败原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in deliveries" :key="row.id" class="border-t border-edge">
            <td class="py-2 text-primary">{{ row.event_type }}</td>
            <td class="py-2 text-secondary">{{ deliveryLabels[row.status] ?? row.status }}</td>
            <td class="py-2 text-secondary tabular-nums">{{ row.attempts }}</td>
            <td class="py-2 text-secondary tabular-nums">{{ row.response_status ?? '—' }}</td>
            <td class="py-2 text-muted">{{ row.last_error || row.error_kind || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="text-xs text-muted">
        投递明细只到"失败类别"这一层：接收方响应正文不入库，因为那可能带对方的数据。
      </p>
    </div>
  </section>
</template>