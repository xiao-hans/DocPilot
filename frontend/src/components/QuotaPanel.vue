<script setup lang="ts">
/**
 * 配额管理（FR-042）：两个轴——文档数与存储字节，0 表示不限。
 *
 * 界面不重新实现判定：额度是否放开、超没超，都由后端说；这里只负责把 PUT 的
 * 「整体覆盖」语义摊开——只想改一个轴时，另一个轴的当前值也一并回传。
 * 配额是护栏不是账本（并发上传可能小幅超出），这句话也印在界面上，免得使用者以为它严格。
 */

import { computed, onMounted, ref } from 'vue'
import { ApiError } from '../api/client'
import { fetchQuota, formatBytes, saveQuota, type QuotaView } from '../api/quota'

const props = defineProps<{ role: string }>()

const view = ref<QuotaView | null>(null)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const documents = ref('')
const storageMb = ref('')

const canWrite = computed(() => props.role === 'owner' || props.role === 'admin')
const unlimited = computed(() => view.value?.unlimited ?? false)

const documentsUsed = computed(() => view.value?.usage.documents ?? 0)
const storageUsed = computed(() => view.value?.usage.storage_bytes ?? 0)

const dirty = computed(() => {
  if (!view.value) return false
  const nextDocs = Number(documents.value || 0)
  const nextBytes = Math.round(Number(storageMb.value || 0) * 1024 * 1024)
  return (
    nextDocs !== view.value.limits.max_documents || nextBytes !== view.value.limits.max_storage_bytes
  )
})

function fillFrom(value: QuotaView) {
  documents.value = String(value.limits.max_documents)
  storageMb.value = value.limits.max_storage_bytes
    ? String(Math.round((value.limits.max_storage_bytes / (1024 * 1024)) * 10) / 10)
    : ''
}

async function load() {
  busy.value = true
  error.value = ''
  try {
    view.value = await fetchQuota()
    fillFrom(view.value)
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.message : '读取配额失败'
  } finally {
    busy.value = false
  }
}

async function submit(maxDocuments: number, maxStorageBytes: number) {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    view.value = await saveQuota({
      max_documents: maxDocuments,
      max_storage_bytes: maxStorageBytes,
    })
    fillFrom(view.value)
    notice.value = maxDocuments === 0 && maxStorageBytes === 0 ? '已恢复为不限' : '额度已保存'
  } catch (exc) {
    error.value = exc instanceof ApiError ? exc.message : '保存配额失败'
  } finally {
    busy.value = false
  }
}

function onSave() {
  const docs = Number(documents.value || 0)
  const bytes = Math.round(Number(storageMb.value || 0) * 1024 * 1024)
  if (!Number.isInteger(docs) || docs < 0) {
    error.value = '文档额度必须是不小于 0 的整数（0 = 不限）'
    return
  }
  if (bytes < 0) {
    error.value = '存储额度不能为负'
    return
  }
  void submit(docs, bytes)
}

onMounted(load)
</script>

<template>
  <section class="space-y-6">
    <header>
      <h2 class="text-2xl font-semibold text-primary">配额</h2>
      <p class="mt-1 text-sm text-secondary">
        额度由租户管理员设定，默认不限。配额是护栏不是账本：并发上传可能小幅超出，判定发生在任何文件写入之前。
      </p>
    </header>

    <p v-if="error" class="rounded-control bg-danger-soft px-3 py-2 text-sm text-danger">
      {{ error }}
    </p>
    <p v-if="notice" class="rounded-control bg-surface-2 px-3 py-2 text-sm text-secondary">
      {{ notice }}
    </p>

    <div v-if="view" class="space-y-4">
      <p class="text-xs text-muted">
        当前状态：<span data-testid="quota-state">{{ unlimited ? '不限（未设置额度）' : '已设置额度' }}</span>
        · 已用文档 {{ documentsUsed }} · 已用存储 {{ formatBytes(storageUsed) }}
      </p>

      <div class="grid gap-4 sm:grid-cols-2">
        <label class="block text-sm" for="quota-documents">
          <span class="text-secondary">文档额度（0 = 不限）</span>
          <input
            id="quota-documents"
            v-model="documents"
            type="number"
            min="0"
            step="1"
            :disabled="!canWrite || busy"
            class="mt-1 w-full rounded-control border border-line bg-base px-3 py-2 text-primary disabled:opacity-60"
          />
          <span class="mt-1 block text-xs text-muted">
            剩余 {{ view.remaining.max_documents ?? '不限' }}
          </span>
        </label>

        <label class="block text-sm" for="quota-storage">
          <span class="text-secondary">存储额度 MiB（0 = 不限）</span>
          <input
            id="quota-storage"
            v-model="storageMb"
            type="number"
            min="0"
            step="1"
            :disabled="!canWrite || busy"
            class="mt-1 w-full rounded-control border border-line bg-base px-3 py-2 text-primary disabled:opacity-60"
          />
          <span class="mt-1 block text-xs text-muted">
            剩余
            {{
              view.remaining.max_storage_bytes === null
                ? '不限'
                : formatBytes(view.remaining.max_storage_bytes)
            }}
          </span>
        </label>
      </div>

      <div class="flex flex-wrap gap-2">
        <button
          type="button"
          class="rounded-control bg-primary px-4 py-2 text-sm text-base disabled:opacity-50"
          :disabled="!canWrite || busy || !dirty"
          @click="onSave"
        >
          保存额度
        </button>
        <button
          type="button"
          class="rounded-control border border-line px-4 py-2 text-sm text-secondary disabled:opacity-50"
          :disabled="!canWrite || busy || unlimited"
          @click="submit(0, 0)"
        >
          恢复不限
        </button>
        <button
          type="button"
          class="rounded-control border border-line px-4 py-2 text-sm text-secondary"
          :disabled="busy"
          @click="load"
        >
          刷新
        </button>
      </div>

      <p v-if="!canWrite" class="text-xs text-muted">
        只有 owner 与 admin 能修改额度；当前角色只读。
      </p>
    </div>

    <p v-else-if="!busy" class="text-sm text-muted">还没有读到配额数据。</p>
  </section>
</template>
