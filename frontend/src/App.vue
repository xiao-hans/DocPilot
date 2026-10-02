<script setup lang="ts">
/**
 * DocAI 演示前端根组件：
 * 三幕状态机 upload → processing → result，切幕淡入 + 上移 400ms（reduced-motion 关闭）。
 */

import { computed, ref, watch } from 'vue'
import BrandMark from './components/BrandMark.vue'
import UploadCard from './components/UploadCard.vue'
import ProcessStatus from './components/ProcessStatus.vue'
import ResultPreview from './components/ResultPreview.vue'
import DownloadButton from './components/DownloadButton.vue'
import AuthGate from './components/AuthGate.vue'
import { INVOICE_FIELDS, type FieldDefinition, type ResultRow } from './api/client'
import { useJobPolling } from './composables/useJobPolling'

type Scene = 'upload' | 'processing' | 'result'

const scene = ref<Scene>('upload')
const jobId = ref('')
const uploadedCount = ref(0) // 上传时记录的文件总数，用于处理幕摘要
const polling = useJobPolling()

function onJobCreated(id: string, fileCount: number) {
  jobId.value = id
  uploadedCount.value = fileCount
  scene.value = 'processing'
  polling.start(id)
}

function onRetry() {
  polling.stop()
  scene.value = 'upload'
}

const failed = computed(() =>
  ['failed', 'timeout', 'connection-error'].includes(polling.state.value.status),
)

const resultRows = computed<ResultRow[] | null>(() => polling.state.value.result?.result ?? null)

const fields = computed<FieldDefinition[]>(() => polling.state.value.job?.fields ?? INVOICE_FIELDS)

const fileCount = computed(() => resultRows.value?.length ?? 0)
const fieldCount = computed(() => fields.value.length)

const warningCount = computed(() => {
  const rows = resultRows.value ?? []
  const rowWarnings = rows.reduce((sum, row) => sum + row.warnings.length, 0)
  return rowWarnings + (polling.state.value.result?.warnings.length ?? 0)
})

function changeTenant(event: Event, selectTenant: (id: string) => void) {
  selectTenant((event.target as HTMLSelectElement).value)
}

// 成功：状态幕停留约 650ms 展示第 5 步完成，再切到结果幕
watch(
  () => polling.state.value.status,
  (status) => {
    if (status === 'succeeded') {
      window.setTimeout(() => {
        scene.value = 'result'
      }, 650)
    }
  },
)
</script>

<template>
  <AuthGate>
    <template #default="{ user, tenant, tenants, selectTenant, logout }">
  <div class="flex min-h-screen flex-col bg-canvas">
    <!-- 顶栏 -->
    <header class="border-b border-edge bg-surface">
      <div class="mx-auto flex w-full max-w-[920px] items-center px-4 py-4 sm:px-8">
        <BrandMark />
        <div class="ml-auto flex items-center gap-3">
          <label class="sr-only" for="active-workspace">当前工作空间</label>
          <select
            id="active-workspace"
            class="max-w-[190px] rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
            :value="tenant.id"
            @change="changeTenant($event, selectTenant)"
          >
            <option v-for="item in tenants" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select>
          <span class="hidden text-xs text-muted sm:inline">{{ user.email }}</span>
          <button
            class="rounded-control border border-edge px-3 py-2 text-xs text-secondary transition-colors hover:border-brand hover:text-brand"
            type="button"
            @click="logout"
          >
            退出
          </button>
        </div>
      </div>
    </header>

    <!-- 三幕主区 -->
    <main class="mx-auto flex w-full max-w-[920px] flex-1 flex-col justify-center px-4 py-16 sm:px-8">
      <Transition name="act" mode="out-in">
        <!-- 第一幕：上传 -->
        <UploadCard v-if="scene === 'upload'" key="upload" @job-created="onJobCreated" />

        <!-- 第二幕：处理状态 -->
        <ProcessStatus
          v-else-if="scene === 'processing'"
          key="processing"
          :progress="polling.state.value.progress"
          :file-count="uploadedCount"
          :failed="failed"
          :error="polling.state.value.error"
          @retry="onRetry"
        />

        <!-- 第三幕：结果 -->
        <section v-else key="result" class="space-y-6">
          <div class="flex flex-wrap items-center justify-between gap-4 rounded-card border border-edge bg-surface px-6 py-5">
            <div class="flex items-center gap-3">
              <span class="flex h-9 w-9 items-center justify-center rounded-full bg-brand">
                <svg
                  class="h-5 w-5 text-on-brand"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="3"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="m5 12.5 4.5 4.5L19 7.5" />
                </svg>
              </span>
              <div>
                <p class="font-display text-lg font-semibold text-primary">处理成功</p>
                <p class="mt-1 font-mono text-xs text-muted tabular-nums">
                  {{ fileCount }} 个文件 · {{ fieldCount }} 个字段
                </p>
              </div>
            </div>
            <DownloadButton :job-id="jobId" />
          </div>

          <ResultPreview :rows="resultRows" :fields="fields" />

          <p v-if="warningCount > 0" class="text-xs text-muted">
            处理完成，含 {{ warningCount }} 条警告
          </p>
        </section>
      </Transition>
    </main>

    <!-- 页脚 -->
    <footer class="border-t border-edge">
      <p class="mx-auto w-full max-w-[920px] px-4 py-4 text-center text-xs text-muted sm:px-8">
        DocPilot · 当前工作空间拥有独立的文档与处理记录
      </p>
    </footer>
  </div>
    </template>
  </AuthGate>
</template>
