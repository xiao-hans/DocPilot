<script setup lang="ts">
/**
 * 复核台：队列 → 当前值编辑 → 保存修订 / 确认 / 退回，附只追加修订链与原文预览。
 * 并发保护走服务端的 expected_revision：冲突返回 409，这里只负责重新拉取而不是覆盖他人。
 */

import { computed, ref, watch } from 'vue'
import {
  confirmResult,
  getResult,
  listResults,
  rejectResult,
  reviseResult,
  versionText,
  type ReviewFilter,
  type ReviewResult,
} from '../api/workspace'
import { ApiError } from '../api/client'

const props = defineProps<{ role: string }>()

const FILTERS: { value: ReviewFilter; label: string }[] = [
  { value: 'machine', label: '待复核' },
  { value: 'revised', label: '已修订' },
  { value: 'rejected', label: '已退回' },
  { value: 'confirmed', label: '已确认' },
  { value: 'all', label: '全部' },
]

const filter = ref<ReviewFilter>('machine')
const queue = ref<ReviewResult[]>([])
const detail = ref<ReviewResult | null>(null)
const draft = ref<Record<string, string>>({})
const reason = ref('')
const busy = ref(false)
const message = ref('')
const error = ref('')
const preview = ref<{ filename: string; char_count: number; text: string } | null>(null)
const showPreview = ref(false)

const canReview = computed(() => ['reviewer', 'admin', 'owner'].includes(props.role))
const confirmed = computed(() => detail.value?.state === 'confirmed')
const dirty = computed(() =>
  detail.value
    ? Object.keys(draft.value).some((name) => String(detail.value!.values[name] ?? '') !== draft.value[name])
    : false,
)

const stateLabels: Record<string, string> = {
  machine: '机器结果',
  revised: '已修订',
  confirmed: '已确认',
  rejected: '已退回',
}

async function loadQueue() {
  error.value = ''
  try {
    queue.value = await listResults({ state: filter.value })
    if (queue.value.length === 0) {
      detail.value = null
      preview.value = null
      showPreview.value = false
      return
    }
    if (!queue.value.some((item) => item.id === detail.value?.id)) {
      await open(queue.value[0]!.id)
    }
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载复核队列失败'
  }
}

async function open(resultId: string) {
  busy.value = true
  error.value = ''
  message.value = ''
  showPreview.value = false
  preview.value = null
  try {
    const loaded = await getResult(resultId)
    detail.value = loaded
    reason.value = ''
    draft.value = Object.fromEntries(
      loaded.fields.map((field) => [field.name, String(loaded.values[field.name] ?? '')]),
    )
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载结果失败'
  } finally {
    busy.value = false
  }
}

/** 数字字段保持数字：复核台按文本编辑，但写回时还原可解析的数值，避免类型漂移。 */
function payload(): Record<string, unknown> {
  const source = detail.value
  if (!source) return {}
  const output: Record<string, unknown> = {}
  for (const field of source.fields) {
    const text = draft.value[field.name] ?? ''
    const original = source.values[field.name]
    if (typeof original === 'number' && text.trim() !== '' && Number.isFinite(Number(text))) {
      output[field.name] = Number(text)
    } else {
      output[field.name] = text
    }
  }
  return output
}

async function run(action: () => Promise<ReviewResult>, successText: string) {
  if (!detail.value) return
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const next = await action()
    detail.value = next
    draft.value = Object.fromEntries(
      next.fields.map((field) => [field.name, String(next.values[field.name] ?? '')]),
    )
    message.value = successText
    // 保存后条目会离开当前队列筛选：让筛选跟随新状态，否则刚改完就看不到自己改的东西
    if (filter.value !== 'all' && filter.value !== next.state) {
      filter.value = next.state
    } else {
      await loadQueue()
    }
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      error.value = '结果已被他人更新，已重新载入当前版本，请核对后再保存'
      await open(detail.value.id)
    } else {
      error.value = err instanceof ApiError ? err.message : '操作失败'
    }
  } finally {
    busy.value = false
  }
}

function save() {
  if (!detail.value) return
  const target = detail.value
  return run(() => reviseResult(target.id, payload(), target.revision_no, reason.value || undefined), '已保存为新修订')
}

function confirm() {
  if (!detail.value) return
  return run(() => confirmResult(detail.value!.id), '已确认')
}

function reject() {
  if (!detail.value) return
  return run(() => rejectResult(detail.value!.id, reason.value), '已退回，需重新修订后再确认')
}

async function togglePreview() {
  if (!detail.value) return
  if (preview.value) {
    showPreview.value = !showPreview.value
    return
  }
  busy.value = true
  try {
    preview.value = await versionText(detail.value.document_version_id)
    showPreview.value = true
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '原文载入失败'
  } finally {
    busy.value = false
  }
}

watch(filter, loadQueue)
loadQueue()
</script>

<template>
  <section class="grid gap-6 lg:grid-cols-[240px_minmax(0,1fr)]">
    <!-- 队列 -->
    <div class="space-y-3">
      <label class="sr-only" for="review-filter">复核队列</label>
      <select
        id="review-filter"
        class="w-full rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
        :value="filter"
        @change="filter = ($event.target as HTMLSelectElement).value as ReviewFilter"
      >
        <option v-for="item in FILTERS" :key="item.value" :value="item.value">{{ item.label }}</option>
      </select>

      <ul class="space-y-2">
        <li v-if="queue.length === 0" class="rounded-control border border-dashed border-edge px-3 py-4 text-xs text-muted">
          此状态下没有待处理结果
        </li>
        <li v-for="item in queue" :key="item.id">
          <button
            class="w-full rounded-control border px-3 py-2 text-left transition-colors"
            :class="item.id === detail?.id ? 'border-brand bg-surface-2' : 'border-edge bg-surface hover:border-brand'"
            type="button"
            @click="open(item.id)"
          >
            <span class="block truncate text-xs font-medium text-primary">{{ item.filename }}</span>
            <span class="mt-1 block font-mono text-[11px] text-muted tabular-nums">
              {{ stateLabels[item.state] }} · v{{ item.revision_no }}
            </span>
          </button>
        </li>
      </ul>
    </div>

    <!-- 详情 -->
    <div class="space-y-4">
      <p v-if="!detail" class="rounded-card border border-dashed border-edge px-6 py-10 text-center text-sm text-muted">
        选择左侧结果开始复核
      </p>

      <div v-else class="space-y-4 rounded-card border border-edge bg-surface p-6">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="font-display text-base font-semibold text-primary">{{ detail.filename }}</p>
            <p class="mt-1 font-mono text-[11px] text-muted">
              {{ detail.doc_type ?? '未分类' }} · 修订 v{{ detail.revision_no }} · {{ stateLabels[detail.state] }}
            </p>
          </div>
          <button
            class="rounded-control border border-edge px-3 py-2 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
            type="button"
            @click="togglePreview"
          >
            {{ showPreview ? '收起原文' : '查看原文' }}
          </button>
        </div>

        <p v-if="preview && showPreview" class="rounded-control border border-edge bg-canvas px-3 py-2">
          <span class="font-mono text-[11px] text-muted">解析文本 {{ preview.char_count }} 字</span>
          <span class="mt-1 block max-h-64 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-secondary">{{ preview.text }}</span>
        </p>

        <p v-if="detail.warnings.length" class="rounded-control border border-edge bg-surface-2 px-3 py-2 text-xs text-secondary">
          {{ detail.warnings.join('；') }}
        </p>

        <dl class="grid gap-3 sm:grid-cols-2">
          <div v-for="field in detail.fields" :key="field.name">
            <dt class="text-[11px] font-medium uppercase tracking-wide text-muted">{{ field.name }}</dt>
            <dd class="mt-1">
              <input
                v-model="draft[field.name]"
                class="w-full rounded-control border border-edge bg-surface px-3 py-2 text-sm text-primary disabled:bg-canvas disabled:text-muted"
                :disabled="!canReview || confirmed"
                :aria-label="field.name"
                type="text"
              />
            </dd>
          </div>
        </dl>

        <div>
          <label class="text-[11px] font-medium uppercase tracking-wide text-muted" for="review-reason">修订理由（可选，退回时必填）</label>
          <textarea
            id="review-reason"
            v-model="reason"
            class="mt-1 w-full rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary disabled:bg-canvas"
            :disabled="!canReview || confirmed"
            maxlength="500"
            rows="2"
          ></textarea>
        </div>

        <div v-if="!canReview" class="rounded-control bg-canvas px-3 py-2 text-xs text-muted">
          当前角色只能查看结果；修订与确认需复核员及以上权限。
        </div>

        <div v-else class="flex flex-wrap gap-2">
          <button
            class="rounded-control bg-brand px-4 py-2 text-xs font-medium text-on-brand transition-opacity disabled:opacity-50"
            type="button"
            :disabled="busy || confirmed || !dirty"
            @click="save"
          >
            保存修订
          </button>
          <button
            class="rounded-control border border-brand px-4 py-2 text-xs font-medium text-brand transition-opacity disabled:opacity-50"
            type="button"
            :disabled="busy || confirmed"
            @click="confirm"
          >
            确认结果
          </button>
          <button
            class="rounded-control border border-edge px-4 py-2 text-xs font-medium text-secondary transition-colors hover:border-danger hover:text-danger disabled:opacity-50"
            type="button"
            :disabled="busy || reason.trim() === ''"
            @click="reject"
          >
            退回重处理
          </button>
        </div>

        <p v-if="message" class="text-xs text-success">{{ message }}</p>
        <p v-if="error" class="text-xs text-danger">{{ error }}</p>

        <div v-if="detail.revisions?.length" class="border-t border-edge pt-4">
          <p class="text-[11px] font-medium uppercase tracking-wide text-muted">修订历史（只追加）</p>
          <ol class="mt-2 space-y-2">
            <li v-for="item in detail.revisions" :key="item.revision_no" class="flex gap-3 text-xs">
              <span class="font-mono text-muted tabular-nums">v{{ item.revision_no }}</span>
              <span :class="item.source === 'human' ? 'text-primary' : 'text-muted'">
                {{ item.source === 'human' ? '人工修订' : '机器提取' }}
                <span v-if="item.reason"> · {{ item.reason }}</span>
              </span>
              <span class="ml-auto font-mono text-[11px] text-muted tabular-nums">{{ item.created_at.slice(0, 16).replace('T', ' ') }}</span>
            </li>
          </ol>
        </div>
      </div>
    </div>
  </section>
</template>
