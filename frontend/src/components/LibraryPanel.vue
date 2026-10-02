<script setup lang="ts">
/**
 * 文档库：关键词检索 + 列表浏览 + 业务标注。
 * 保留状态属治理面，只有 admin/owner 可改（后端同一规则，这里只是不让误点）。
 */

import { computed, ref } from 'vue'
import {
  listDocuments,
  searchWorkspace,
  updateDocumentMetadata,
  type DocumentItem,
  type SearchHit,
} from '../api/workspace'
import { ApiError } from '../api/client'

const props = defineProps<{ role: string }>()

const documents = ref<DocumentItem[]>([])
const total = ref(0)
const offset = ref(0)
const pageSize = 10
const query = ref('')
const kind = ref<'all' | 'filename' | 'text' | 'field'>('all')
const hits = ref<SearchHit[]>([])
const searchNote = ref('')
const labelDraft = ref<Record<string, string>>({})
const busy = ref(false)
const error = ref('')
const notice = ref('')

const canGovern = computed(() => ['admin', 'owner'].includes(props.role))
const canAnnotate = computed(() => ['member', 'admin', 'owner'].includes(props.role))

const stateLabels: Record<string, string> = {
  machine: '机器结果',
  revised: '已修订',
  confirmed: '已确认',
  rejected: '已退回',
}

async function load(target = offset.value) {
  busy.value = true
  error.value = ''
  try {
    const page = await listDocuments({ limit: pageSize, offset: target })
    documents.value = page.items
    total.value = page.total
    offset.value = target
    for (const item of page.items) {
      labelDraft.value[item.id] = item.labels.join('、')
    }
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '文档库加载失败'
  } finally {
    busy.value = false
  }
}

async function runSearch() {
  notice.value = ''
  error.value = ''
  const term = query.value.trim()
  if (term.length < 2) {
    hits.value = []
    searchNote.value = '关键词至少 2 个字符'
    return
  }
  busy.value = true
  try {
    hits.value = await searchWorkspace(term, kind.value)
    searchNote.value = `${hits.value.length} 条命中`
  } catch (err) {
    hits.value = []
    searchNote.value = ''
    error.value = err instanceof ApiError ? err.message : '检索失败'
  } finally {
    busy.value = false
  }
}

function clearSearch() {
  query.value = ''
  hits.value = []
  searchNote.value = ''
}

function nameOf(documentId: string): string {
  const found = documents.value.find((item) => item.id === documentId)
  return found?.versions[0]?.original_filename ?? documentId.slice(0, 8)
}

async function saveLabels(item: DocumentItem) {
  const labels = (labelDraft.value[item.id] ?? '')
    .split(/[、,]/)
    .map((label) => label.trim())
    .filter((label) => label !== '')
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const updated = await updateDocumentMetadata(item.id, { labels })
    item.labels = updated.labels
    item.doc_type = updated.doc_type
    notice.value = `已更新标签（最多 10 个，每个 30 字内）`
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '标签保存失败'
    await load()
  } finally {
    busy.value = false
  }
}

async function setRetention(item: DocumentItem, value: string) {
  busy.value = true
  error.value = ''
  try {
    const updated = await updateDocumentMetadata(item.id, { retention_status: value })
    item.retention_status = updated.retention_status
    notice.value = `${nameOf(item.id)} 保留状态已置为 ${value}`
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '保留状态更新失败'
    await load()
  } finally {
    busy.value = false
  }
}

function formatBytes(value: number): string {
  return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.round(value / 1024)} KB`
}

load(0)
</script>

<template>
  <section class="space-y-5">
    <!-- 检索 -->
    <div class="flex flex-wrap items-center gap-2 rounded-card border border-edge bg-surface px-4 py-3">
      <label class="sr-only" for="library-q">关键词</label>
      <input
        id="library-q"
        v-model="query"
        class="min-w-[180px] flex-1 rounded-control border border-edge bg-canvas px-3 py-2 text-sm text-primary"
        placeholder="搜索文件名、原文或已提取字段值"
        type="search"
        @keyup.enter="runSearch"
      />
      <label class="sr-only" for="library-kind">来源</label>
      <select
        id="library-kind"
        v-model="kind"
        class="rounded-control border border-edge bg-canvas px-2 py-2 text-xs text-primary"
      >
        <option value="all">全部来源</option>
        <option value="filename">文件名</option>
        <option value="text">原文</option>
        <option value="field">字段值</option>
      </select>
      <button class="rounded-control bg-brand px-4 py-2 text-xs font-medium text-on-brand" type="button" @click="runSearch">
        检索
      </button>
      <button
        v-if="hits.length || query"
        class="rounded-control border border-edge px-3 py-2 text-xs text-secondary hover:border-brand hover:text-brand"
        type="button"
        @click="clearSearch"
      >
        清除
      </button>
      <span v-if="searchNote" class="font-mono text-[11px] text-muted tabular-nums">{{ searchNote }}</span>
    </div>

    <ul v-if="hits.length" class="space-y-2">
      <li v-for="(hit, index) in hits" :key="`${hit.document_id}-${hit.kind}-${hit.field_name}-${index}`">
        <div class="rounded-control border border-edge bg-surface px-4 py-3">
          <p class="text-xs font-medium text-primary">
            {{ nameOf(hit.document_id) }}
            <span class="ml-2 font-mono text-[11px] text-muted">
              {{ hit.kind === 'field' ? `字段 ${hit.field_name}` : hit.kind === 'text' ? '解析原文' : '文件名' }}
            </span>
          </p>
          <p class="mt-1 break-words text-[11px] leading-relaxed text-secondary">{{ hit.snippet }}</p>
        </div>
      </li>
    </ul>

    <!-- 列表 -->
    <div class="space-y-3">
      <div class="flex items-center justify-between">
        <p class="font-display text-sm font-semibold text-primary">全部文档 · {{ total }} 份</p>
        <div class="flex gap-2">
          <button
            class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary disabled:opacity-40"
            type="button"
            :disabled="offset === 0 || busy"
            @click="load(Math.max(0, offset - pageSize))"
          >
            上一页
          </button>
          <button
            class="rounded-control border border-edge px-3 py-1.5 text-xs text-secondary disabled:opacity-40"
            type="button"
            :disabled="offset + pageSize >= total || busy"
            @click="load(offset + pageSize)"
          >
            下一页
          </button>
        </div>
      </div>

      <p v-if="error" class="rounded-control border border-danger px-3 py-2 text-xs text-danger">{{ error }}</p>
      <p v-if="notice" class="text-xs text-success">{{ notice }}</p>

      <ul class="space-y-2">
        <li v-for="item in documents" :key="item.id" class="rounded-card border border-edge bg-surface px-4 py-3">
          <div class="flex flex-wrap items-start gap-x-4 gap-y-2">
            <div class="min-w-[200px] flex-1">
              <p class="truncate text-sm font-medium text-primary">{{ item.versions[0]?.original_filename ?? '未知文件' }}</p>
              <p class="mt-1 font-mono text-[11px] text-muted tabular-nums">
                {{ item.versions.length }} 个版本 ·
                {{ formatBytes(item.versions.reduce((sum, version) => sum + version.size_bytes, 0)) }} ·
                {{ item.created_at.slice(0, 10) }} ·
                {{ item.result_states.map((state) => stateLabels[state] ?? state).join('/') || '无提取结果' }}
              </p>
            </div>

            <div class="w-full sm:w-56">
              <label class="text-[11px] uppercase tracking-wide text-muted" :for="`labels-${item.id}`">标签</label>
              <div class="mt-1 flex gap-2">
                <input
                  :id="`labels-${item.id}`"
                  v-model="labelDraft[item.id]"
                  class="w-full rounded-control border border-edge bg-canvas px-2 py-1.5 text-xs text-primary disabled:opacity-50"
                  :disabled="!canAnnotate || busy"
                  type="text"
                />
                <button
                  class="shrink-0 rounded-control border border-edge px-2 py-1.5 text-[11px] text-secondary hover:border-brand hover:text-brand disabled:opacity-40"
                  type="button"
                  :disabled="!canAnnotate || busy"
                  @click="saveLabels(item)"
                >
                  保存
                </button>
              </div>
            </div>

            <div>
              <label class="text-[11px] uppercase tracking-wide text-muted" :for="`retention-${item.id}`">保留状态</label>
              <select
                :id="`retention-${item.id}`"
                class="mt-1 block rounded-control border border-edge bg-canvas px-2 py-1.5 text-xs text-primary disabled:opacity-50"
                :value="item.retention_status"
                :disabled="!canGovern || busy"
                @change="setRetention(item, ($event.target as HTMLSelectElement).value)"
              >
                <option value="active">在用</option>
                <option value="purge_scheduled">待清除</option>
                <option value="purged">已清除</option>
              </select>
            </div>
          </div>
        </li>
      </ul>
    </div>
  </section>
</template>
