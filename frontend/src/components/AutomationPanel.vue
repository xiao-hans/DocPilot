<script setup lang="ts">
/**
 * 事件规则管理：订阅哪些事件、满足什么条件、执行什么动作，以及手动消费与运行日志。
 * 动作类型只开放后端白名单里的两种（不含自动确认、自动删除、外部调用），
 * 条件键同样取自 `GET /automation/meta`，界面不自己抄一份清单。
 */

import { computed, onMounted, ref } from 'vue'
import {
  createRule,
  dispatchEvents,
  getRuleMeta,
  listNotifications,
  listRules,
  listRuns,
  runRule,
  updateRule,
  type NotificationDto,
  type RuleDto,
  type RuleMeta,
  type RuleRunDto,
} from '../api/admin'
import { ApiError } from '../api/client'

const props = defineProps<{ role: string }>()

const rules = ref<RuleDto[]>([])
const runs = ref<RuleRunDto[]>([])
const notifications = ref<NotificationDto[]>([])
const meta = ref<RuleMeta | null>(null)
const busy = ref(false)
const error = ref('')
const notice = ref('')

const form = ref<{
  name: string
  event_type: string
  conditionKey: string
  conditionValue: string
  actionType: string
  actionParam: string
}>({
  name: '',
  event_type: '',
  conditionKey: '',
  conditionValue: '',
  actionType: '',
  actionParam: '',
})

const canManage = computed(() => ['admin', 'owner'].includes(props.role))
const conditionKeys = computed(() =>
  meta.value && form.value.event_type ? (meta.value.condition_keys[form.value.event_type] ?? []) : [],
)
const actionLabel = computed(() => (form.value.actionType === 'tag_document' ? '标签名' : '通知标题'))

async function load() {
  busy.value = true
  error.value = ''
  try {
    const [rulePage, runPage, notifyPage, metaData] = await Promise.all([
      listRules(),
      listRuns(10),
      canManage.value ? listNotifications(10) : Promise.resolve({ total: 0, notifications: [] }),
      meta.value ? Promise.resolve(meta.value) : getRuleMeta(),
    ])
    rules.value = rulePage.rules
    runs.value = runPage.runs
    notifications.value = notifyPage.notifications
    meta.value = metaData
    if (!form.value.event_type) form.value.event_type = metaData.event_types[0] ?? ''
    if (!form.value.actionType) form.value.actionType = metaData.action_types[0] ?? ''
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '规则数据加载失败'
  } finally {
    busy.value = false
  }
}

function buildCondition(): Record<string, unknown> | null {
  if (!form.value.conditionKey || !form.value.conditionValue.trim()) return null
  const raw = form.value.conditionValue.trim()
  const numeric = Number(raw)
  return { [form.value.conditionKey]: Number.isFinite(numeric) && raw !== '' ? numeric : raw }
}

async function submit() {
  if (!form.value.name.trim()) {
    error.value = '请填写规则名称'
    return
  }
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await createRule({
      name: form.value.name.trim(),
      event_type: form.value.event_type,
      condition: buildCondition(),
      action: { type: form.value.actionType as 'tag_document', params: { [paramKey()]: form.value.actionParam } },
    })
    notice.value = `规则「${form.value.name.trim()}」已创建并启用`
    form.value.name = ''
    form.value.actionParam = ''
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '规则创建失败'
  } finally {
    busy.value = false
  }
}

function paramKey(): string {
  return form.value.actionType === 'tag_document' ? 'label' : 'subject'
}

async function toggle(rule: RuleDto) {
  error.value = ''
  notice.value = ''
  try {
    await updateRule(rule.id, { status: rule.status === 'active' ? 'paused' : 'active' })
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '规则状态更新失败'
  }
}

async function consume(onlyRule?: string) {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const summary = onlyRule
      ? await runRule(onlyRule)
      : await dispatchEvents()
    notice.value = `消费 ${summary.consumed} 条事件：执行 ${summary.applied} · 跳过 ${summary.skipped} · 失败 ${summary.failed}`
    await load()
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '事件消费失败'
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="space-y-6" aria-label="事件规则">
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h2 class="font-display text-xl font-semibold text-primary">事件规则</h2>
        <p class="mt-1 text-xs text-muted">
          规则只在手动"消费事件"时执行；通知只登记不投递，动作类型受后端白名单限制。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button
          class="rounded-control border border-edge px-3 py-2 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
          type="button"
          :disabled="busy"
          @click="load"
        >
          刷新
        </button>
        <button
          v-if="canManage"
          class="rounded-control bg-brand px-4 py-2 text-xs font-medium text-on-brand transition-colors hover:bg-brand-soft"
          type="button"
          data-testid="dispatch-events"
          :disabled="busy"
          @click="consume()"
        >
          消费事件
        </button>
      </div>
    </div>

    <p v-if="error" class="rounded-card border border-danger/40 bg-danger/10 px-4 py-3 text-xs text-danger" role="alert">
      {{ error }}
    </p>
    <p v-if="notice" class="rounded-card border border-edge bg-surface-2 px-4 py-3 text-xs text-secondary" role="status">
      {{ notice }}
    </p>

    <div v-if="canManage" class="space-y-3 rounded-card border border-edge bg-surface px-5 py-4">
      <p class="text-xs font-semibold text-primary">新建规则</p>
      <div class="flex flex-wrap items-center gap-2">
        <input
          v-model="form.name"
          class="w-[180px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
          placeholder="规则名称"
          type="text"
        />
        <select
          v-model="form.event_type"
          class="rounded-control border border-edge bg-surface px-2 py-2 text-xs text-primary"
          aria-label="订阅事件"
        >
          <option v-for="item in meta?.event_types ?? []" :key="item" :value="item">{{ item }}</option>
        </select>
        <select
          v-model="form.conditionKey"
          class="rounded-control border border-edge bg-surface px-2 py-2 text-xs text-primary"
          aria-label="条件键"
        >
          <option value="">无条件</option>
          <option v-for="item in conditionKeys" :key="item" :value="item">{{ item }}</option>
        </select>
        <input
          v-if="form.conditionKey"
          v-model="form.conditionValue"
          class="w-[120px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
          placeholder="条件值"
          type="text"
        />
        <select
          v-model="form.actionType"
          class="rounded-control border border-edge bg-surface px-2 py-2 text-xs text-primary"
          aria-label="动作类型"
        >
          <option v-for="item in meta?.action_types ?? []" :key="item" :value="item">{{ item }}</option>
        </select>
        <input
          v-model="form.actionParam"
          class="min-w-[160px] flex-1 rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
          :placeholder="actionLabel"
          type="text"
        />
        <button
          class="rounded-control bg-brand px-4 py-2 text-xs font-medium text-on-brand transition-colors hover:bg-brand-soft"
          type="button"
          :disabled="busy"
          data-testid="create-rule"
          @click="submit"
        >
          保存规则
        </button>
      </div>
    </div>

    <div class="space-y-3">
      <p class="text-xs font-semibold text-primary">规则（{{ rules.length }}）</p>
      <p v-if="!rules.length" class="text-xs text-muted">还没有规则。规则默认立即启用，可在列表里暂停。</p>
      <ul class="space-y-2">
        <li
          v-for="rule in rules"
          :key="rule.id"
          class="flex flex-wrap items-center gap-3 rounded-card border border-edge bg-surface px-5 py-3"
        >
          <span class="text-xs text-primary">{{ rule.name }}</span>
          <span class="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-xs text-secondary">{{ rule.event_type }}</span>
          <span class="font-mono text-xs text-muted">
            {{ rule.action.type }}
            <template v-if="rule.condition"> · {{ JSON.stringify(rule.condition) }}</template>
            · v{{ rule.version }}
          </span>
          <span class="font-mono text-xs text-muted tabular-nums">
            执行 {{ rule.runs?.applied ?? 0 }} · 跳过 {{ rule.runs?.skipped ?? 0 }} · 失败 {{ rule.runs?.failed ?? 0 }}
          </span>
          <span v-if="canManage" class="ml-auto flex items-center gap-2">
            <button
              class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
              type="button"
              :disabled="busy"
              @click="consume(rule.id)"
            >
              只跑这条
            </button>
            <button
              class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
              type="button"
              :disabled="busy"
              :data-testid="`toggle-rule-${rule.id}`"
              @click="toggle(rule)"
            >
              {{ rule.status === 'active' ? '暂停' : '启用' }}
            </button>
          </span>
        </li>
      </ul>
    </div>

    <div class="space-y-3">
      <p class="text-xs font-semibold text-primary">最近运行（{{ runs.length }}）</p>
      <p v-if="!runs.length" class="text-xs text-muted">还没有运行记录。消费事件后这里会出现每条事件的结果。</p>
      <ul class="space-y-1">
        <li v-for="run in runs" :key="run.id" class="flex flex-wrap items-center gap-3">
          <span class="font-mono text-xs text-muted tabular-nums">
            {{ run.created_at.slice(11, 19) }}
          </span>
          <span class="text-xs text-secondary">{{ run.rule_name }}</span>
          <span class="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-xs text-secondary">{{ run.status }}</span>
          <span class="font-mono text-xs text-muted">{{ run.event_type }}</span>
          <span v-if="run.detail" class="text-xs text-muted">{{ run.detail }}</span>
        </li>
      </ul>
    </div>

    <div v-if="canManage" class="space-y-3">
      <p class="text-xs font-semibold text-primary">待投递通知（{{ notifications.length }}）</p>
      <p v-if="!notifications.length" class="text-xs text-muted">当前没有待投递通知。</p>
      <ul class="space-y-1">
        <li v-for="item in notifications" :key="item.id" class="flex flex-wrap items-center gap-3">
          <span class="font-mono text-xs text-muted tabular-nums">{{ item.created_at.slice(5, 16) }}</span>
          <span class="text-xs text-secondary">{{ item.body }}</span>
          <span class="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-xs text-secondary">{{ item.status }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>
