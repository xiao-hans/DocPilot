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
import ReviewPanel from './components/ReviewPanel.vue'
import LibraryPanel from './components/LibraryPanel.vue'
import TemplatesPanel from './components/TemplatesPanel.vue'
import TeamPanel from './components/TeamPanel.vue'
import AutomationPanel from './components/AutomationPanel.vue'
import OperationsPanel from './components/OperationsPanel.vue'
import QuotaPanel from './components/QuotaPanel.vue'
import WebhooksPanel from './components/WebhooksPanel.vue'
import AuthGate from './components/AuthGate.vue'
import { INVOICE_FIELDS, type FieldDefinition, type ResultRow } from './api/client'
import { useJobPolling } from './composables/useJobPolling'

type Scene = 'upload' | 'processing' | 'result'
type View =
  | 'process'
  | 'review'
  | 'library'
  | 'templates'
  | 'team'
  | 'automation'
  | 'webhooks'
  | 'quota'
  | 'operations'

const view = ref<View>('process')
const VIEWS: {
  value: View
  label: string
  platformOnly?: boolean
  adminOnly?: boolean
  hiddenForReviewer?: boolean
}[] = [
  { value: 'process', label: '处理' },
  { value: 'review', label: '复核' },
  { value: 'library', label: '文档库' },
  { value: 'templates', label: '模板' },
  { value: 'team', label: '团队' },
  { value: 'automation', label: '自动化' },
  // Webhook 读面对 member 开放（排障要能看到失败投递），写面只 owner/admin；
  // 与后端 `app/contexts/webhooks/api.py` 的 READ_ROLES/WRITE_ROLES 一致。
  // `reviewer` 不在读面里，因此这里也不给入口——藏入口不是权限控制，真把关在后端。
  { value: 'webhooks', label: 'Webhook', hiddenForReviewer: true },
  // 配额是租户自己的设置，写权限在后端 require_roles 上把关；
  // 这里按角色藏入口，是为了不让 member 看见一个点进去只能报错的按钮。
  { value: 'quota', label: '配额', adminOnly: true },
  // 运营视图的主体是跨租户数据，入口只给平台身份看；真正的把关在后端
  // `require_platform_admin`（非平台身份请求拿到 403），这里藏入口不是权限控制。
  { value: 'operations', label: '运营', platformOnly: true },
]

function visibleViews(isPlatformAdmin: boolean, role: string) {
  const canManage = role === 'owner' || role === 'admin'
  return VIEWS.filter(
    (item) =>
      (!item.platformOnly || isPlatformAdmin) &&
      (!item.adminOnly || canManage) &&
      (!item.hiddenForReviewer || role !== 'reviewer'),
  )
}
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

// 部分成功是独立终态：结果可用，但必须让用户看见有文件失败（FR-020）
const doneTitle = computed(() => (polling.state.value.status === 'partial' ? '部分处理成功' : '处理成功'))

function changeTenant(event: Event, selectTenant: (id: string) => void) {
  selectTenant((event.target as HTMLSelectElement).value)
}

// 成功：状态幕停留约 650ms 展示第 5 步完成，再切到结果幕
watch(
  () => polling.state.value.status,
  (status) => {
    if (status === 'succeeded' || status === 'partial') {
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
      <div class="mx-auto flex w-full max-w-[1100px] items-center px-4 py-4 sm:px-8">
        <BrandMark />
        <nav class="ml-6 hidden items-center gap-1 sm:flex" aria-label="工作区">
          <button
            v-for="item in visibleViews(user.is_platform_admin, tenant.role)"
            :key="item.value"
            class="rounded-control px-3 py-2 text-xs transition-colors"
            :class="view === item.value ? 'bg-surface-2 text-primary' : 'text-secondary hover:text-primary'"
            type="button"
            @click="view = item.value"
          >
            {{ item.label }}
          </button>
        </nav>
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
      <nav class="flex items-center gap-1 border-t border-edge px-4 py-2 sm:hidden" aria-label="工作区（窄屏）">
        <button
          v-for="item in visibleViews(user.is_platform_admin, tenant.role)"
          :key="`mobile-${item.value}`"
          class="rounded-control px-3 py-1.5 text-xs transition-colors"
          :class="view === item.value ? 'bg-surface-2 text-primary' : 'text-secondary'"
          type="button"
          @click="view = item.value"
        >
          {{ item.label }}
        </button>
      </nav>
    </header>

    <!-- 复核、文档库与三个管理面 -->
    <main v-if="view !== 'process'" class="mx-auto w-full max-w-[1100px] px-4 py-10 sm:px-8">
      <ReviewPanel v-if="view === 'review'" :role="tenant.role" />
      <LibraryPanel v-else-if="view === 'library'" :role="tenant.role" />
      <TemplatesPanel v-else-if="view === 'templates'" :role="tenant.role" />
      <TeamPanel v-else-if="view === 'team'" :role="tenant.role" />
      <AutomationPanel v-else-if="view === 'automation'" :role="tenant.role" />
      <WebhooksPanel v-else-if="view === 'webhooks'" :role="tenant.role" />
      <QuotaPanel v-else-if="view === 'quota'" :role="tenant.role" />
      <OperationsPanel v-else :platform-admin="user.is_platform_admin" />
    </main>

    <!-- 处理主流程 -->
    <main v-else class="mx-auto flex w-full max-w-[920px] flex-1 flex-col justify-center px-4 py-16 sm:px-8">
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
                <p class="font-display text-lg font-semibold text-primary">{{ doneTitle }}</p>
                <p class="mt-1 font-mono text-xs text-muted tabular-nums">
                  {{ fileCount }} 个文件 · {{ fieldCount }} 个字段
                </p>
              </div>
            </div>
            <div class="flex items-center gap-3">
              <DownloadButton :job-id="jobId" />
              <!--
                「再处理一份」（维护轮 M6）：结果幕原先只有下载入口，要开新任务只能刷新页面。
                `onRetry()` 本来就在（失败分支的"重新选择文件"用它切回上传幕），这里复用同一个
                处理函数，不新增状态。用次要按钮样式：主行动仍是下载。
              -->
              <button
                type="button"
                class="rounded-control border border-line px-4 py-2.5 font-display text-sm font-semibold text-secondary transition-colors duration-fast hover:border-brand hover:text-primary"
                @click="onRetry"
              >
                再处理一份
              </button>
            </div>
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
