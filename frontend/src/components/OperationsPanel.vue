<script setup lang="ts">
/**
 * 运营视图：平台用量与失败分布，加上本租户的用量与失败明细。
 * 对应 DOC-DD-007 §5 的四个只读端点；这里不做任何写操作。
 *
 * 界面刻意分成两层，因为它就是在演示后端那条边界：
 * - 平台区块跨租户，后端只给计数与错误代号，所以这里没有"点开看某租户失败原因"的入口；
 *   想查原文必须去下面的本租户区块，那里读的是当前工作空间自己的数据。
 * - `platformAdmin` 只决定要不要**请求**平台端点。隐藏入口不是权限控制——真正的把关在
 *   后端 `require_platform_admin`，非平台身份拿到的是 403。
 */

import { computed, onMounted, ref } from 'vue'
import { ApiError } from '../api/client'
import {
  fetchPlatformFailures,
  fetchPlatformUsage,
  fetchTenantFailures,
  fetchTenantUsage,
  formatBytes,
  type PlatformFailuresDto,
  type PlatformUsageDto,
  type TenantFailuresDto,
  type TenantUsageDto,
} from '../api/operations'

const props = defineProps<{ platformAdmin: boolean }>()

const WINDOWS = [7, 30, 90] as const

const days = ref<number>(30)
const busy = ref(false)
const error = ref('')
const usage = ref<TenantUsageDto | null>(null)
const failures = ref<TenantFailuresDto | null>(null)
const platform = ref<PlatformUsageDto | null>(null)
const platformFailures = ref<PlatformFailuresDto | null>(null)

const jobStatusLabels: Record<string, string> = {
  queued: '排队中',
  processing: '处理中',
  succeeded: '成功',
  partial: '部分成功',
  failed: '失败',
}

const resultStateLabels: Record<string, string> = {
  machine: '机器值',
  revised: '已修订',
  confirmed: '已确认',
  rejected: '已驳回',
}

// 恶意文件检测的四种事实（DOC-DD-024）。`unscanned` 应单独一格：它不等于"干净"，
// 混同会让"能力没开"显示成"全部通过"。界面因此不把 0 值折叠掉，缺的状态由后端补 0。
const malwareStatusLabels: Record<string, string> = {
  clean: '已扫描且干净',
  infected: '发现威胁',
  error: '引擎不可用',
  unscanned: '未扫描',
}

const tenantTotalJobs = computed(() => platform.value?.tenants.length ?? 0)

function statusSummary(byStatus: Record<string, number>, labels: Record<string, string>): string {
  return Object.entries(byStatus)
    .filter(([, count]) => count > 0)
    .map(([status, count]) => `${labels[status] ?? status} ${count}`)
    .join(' · ')
}

async function load() {
  busy.value = true
  error.value = ''
  try {
    const [ownUsage, ownFailures] = await Promise.all([
      fetchTenantUsage(days.value),
      fetchTenantFailures(days.value),
    ])
    usage.value = ownUsage
    failures.value = ownFailures

    if (props.platformAdmin) {
      const [platformUsage, platformFailureView] = await Promise.all([
        fetchPlatformUsage(days.value),
        fetchPlatformFailures(days.value),
      ])
      platform.value = platformUsage
      platformFailures.value = platformFailureView
    } else {
      platform.value = null
      platformFailures.value = null
    }
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '运营数据加载失败'
  } finally {
    busy.value = false
  }
}

function shorten(id: string): string {
  return id.length <= 10 ? id : `${id.slice(0, 8)}…`
}

onMounted(load)
</script>

<template>
  <section class="space-y-6" aria-label="运营视图">
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h2 class="font-display text-xl font-semibold text-primary">用量与失败</h2>
        <p class="mt-1 text-xs text-muted">
          数字全部由业务表现算，不建第二真相；错误分布只到代号为止，原文留在各自租户里看。
        </p>
      </div>
      <div class="flex items-end gap-3">
        <div>
          <label class="sr-only" for="ops-window">统计窗口</label>
          <select
            id="ops-window"
            v-model.number="days"
            class="rounded-control border border-edge bg-surface px-3 py-2 text-xs text-primary"
            @change="load"
          >
            <option v-for="option in WINDOWS" :key="option" :value="option">最近 {{ option }} 天</option>
          </select>
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
    </div>

    <p v-if="busy" class="text-xs text-muted">加载中…</p>
    <p
      v-if="error"
      class="rounded-card border border-danger/40 bg-danger/10 px-4 py-3 text-xs text-danger"
      role="alert"
    >
      {{ error }}
    </p>

    <!-- 平台层：跨租户，只有计数与代号 -->
    <div
      v-if="platformAdmin && platform"
      class="space-y-3 rounded-card border border-edge bg-surface px-5 py-4"
      role="region"
      aria-label="平台用量"
    >
      <h3 class="text-sm font-semibold text-primary">平台用量</h3>
      <p data-testid="platform-totals" class="text-xs text-secondary">
        {{ platform.total_tenants }} 个租户 · 文档 {{ platform.totals.documents }} ·
        存储 {{ formatBytes(platform.totals.storage_bytes) }} · 待投递事件
        {{ platform.totals.pending_events }} · 威胁待处置
        {{ platform.totals.malware_flagged }}
      </p>
      <div class="overflow-x-auto">
        <table class="w-full border-collapse text-left text-xs">
          <caption class="sr-only">按租户的用量聚合，按文档数倒序</caption>
          <thead>
            <tr class="text-muted">
              <th class="py-2 pr-4 font-medium" scope="col">租户</th>
              <th class="py-2 pr-4 font-medium" scope="col">文档</th>
              <th class="py-2 pr-4 font-medium" scope="col">存储</th>
              <th class="py-2 pr-4 font-medium" scope="col">任务</th>
              <th class="py-2 pr-4 font-medium" scope="col">窗口内失败</th>
              <th class="py-2 pr-4 font-medium" scope="col">待投递事件</th>
              <th class="py-2 font-medium" scope="col">规则失败</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in platform.tenants"
              :key="row.tenant_id"
              class="border-t border-edge text-secondary"
              data-testid="platform-tenant-row"
            >
              <td class="py-2 pr-4 font-mono">{{ shorten(row.tenant_id) }}</td>
              <td class="py-2 pr-4">{{ row.documents }}</td>
              <td class="py-2 pr-4">{{ formatBytes(row.storage_bytes) }}</td>
              <td class="py-2 pr-4">
                {{ statusSummary(row.jobs, jobStatusLabels) || '无任务' }}
              </td>
              <td class="py-2 pr-4">{{ row.jobs_failed_in_window }}</td>
              <td class="py-2 pr-4">{{ row.pending_events }}</td>
              <td class="py-2">{{ row.rule_runs_failed }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="text-xs text-muted">
        列出 {{ tenantTotalJobs }} / {{ platform.total_tenants }} 个租户。平台身份不等于客户数据读取权：
        这一层看不到任何文件名与错误原文。
      </p>
    </div>

    <div
      v-if="platformAdmin && platformFailures"
      class="space-y-3 rounded-card border border-edge bg-surface px-5 py-4"
      role="region"
      aria-label="平台失败分布"
    >
      <h3 class="text-sm font-semibold text-primary">失败分布（按错误代号）</h3>
      <p data-testid="platform-failure-summary" class="text-xs text-secondary">
        窗口内 {{ platformFailures.scanned_rows }} 条失败任务 · 涉及
        {{ platformFailures.tenants_with_failures }} 个租户 · {{ platformFailures.codes.length }} 类错误
      </p>
      <p
        v-if="platformFailures.truncated"
        class="rounded-card border border-warn/40 bg-warn/10 px-4 py-2 text-xs text-warn"
        role="status"
      >
        只扫了最近 {{ platformFailures.scan_cap }} 条失败记录，分布不完整。
      </p>
      <p v-else-if="!platformFailures.codes.length" class="text-xs text-muted">
        窗口内没有失败任务。
      </p>
      <ul v-else class="space-y-2">
        <li
          v-for="code in platformFailures.codes"
          :key="code.code"
          class="flex flex-wrap items-center justify-between gap-3 text-xs"
          data-testid="platform-failure-code"
        >
          <span class="text-secondary">
            {{ code.label }}
            <span class="ml-2 font-mono text-muted">{{ code.code }}</span>
          </span>
          <span class="text-muted">
            {{ code.jobs }} 次 · {{ code.tenants_affected }} 个租户
            <span v-if="code.last_seen" class="ml-2">最近 {{ code.last_seen.slice(0, 16) }}</span>
          </span>
        </li>
      </ul>
      <p class="text-xs text-muted">
        代号是按错误文本形状做的近似归类，认不出的只给指纹；权威始终是下面这份本租户明细里的原文。
      </p>
    </div>

    <!-- 租户层：自己的数据，含原文 -->
    <div
      v-if="usage"
      class="space-y-3 rounded-card border border-edge bg-surface px-5 py-4"
      role="region"
      aria-label="本工作空间用量"
    >
      <h3 class="text-sm font-semibold text-primary">本工作空间用量</h3>
      <p data-testid="tenant-usage-summary" class="text-xs text-secondary">
        文档 {{ usage.documents.total }} · 版本 {{ usage.file_versions.total }} ·
        存储 {{ formatBytes(usage.file_versions.storage_bytes) }} · 任务 {{ usage.jobs.total }}
        （尝试 {{ usage.jobs.attempts }} 次）· 结果 {{ usage.results.total }} ·
        检索条目 {{ usage.search_entries }}
      </p>
      <p class="text-xs text-muted">
        任务：{{ statusSummary(usage.jobs.by_status, jobStatusLabels) || '无' }} · 结果：{{
          statusSummary(usage.results.by_state, resultStateLabels) || '无'
        }} · 人工修订 {{ usage.results.human_revisions }} 次 · 活跃规则
        {{ usage.automation.rules_active }} 条 · 待投递事件 {{ usage.automation.pending_events }}
      </p>
      <p data-testid="tenant-malware-summary" class="text-xs text-secondary">
        恶意文件检测：{{ statusSummary(usage.malware.by_status, malwareStatusLabels) || '无版本' }}（版本 {{ usage.malware.total }}）
      </p>
      <p class="text-xs text-muted">
        恶意文件检测默认关闭；此时全部版本都落在"未扫描"里。那是能力没开的证据，
        不是扫描通过的结论。开启方式见 DOC-DD-024。
      </p>
    </div>

    <div
      v-if="failures"
      class="space-y-3 rounded-card border border-edge bg-surface px-5 py-4"
      role="region"
      aria-label="本工作空间失败明细"
    >
      <h3 class="text-sm font-semibold text-primary">本工作空间失败明细</h3>
      <p data-testid="tenant-failure-summary" class="text-xs text-secondary">
        窗口内失败任务 {{ failures.jobs.total }} 条 · 失败规则运行
        {{ failures.rule_runs.total }} 条
      </p>
      <p v-if="!failures.jobs.items.length" class="text-xs text-muted">
        这个窗口里没有失败任务。
      </p>
      <ul v-else class="space-y-2">
        <li
          v-for="item in failures.jobs.items"
          :key="item.job_id"
          class="rounded-card border border-edge bg-surface-2 px-4 py-3"
          data-testid="tenant-failure-item"
        >
          <p class="text-xs text-primary">
            任务 <span class="font-mono">{{ shorten(item.job_id) }}</span> ·
            {{ jobStatusLabels[item.status] ?? item.status }} · 尝试
            {{ item.attempt_count }} 次 · <span class="text-brand">{{ item.label }}</span>
          </p>
          <p class="mt-1 text-xs text-muted break-all">{{ item.error || '无错误信息记录' }}</p>
        </li>
      </ul>
      <ul v-if="failures.rule_runs.items.length" class="space-y-2">
        <li
          v-for="run in failures.rule_runs.items"
          :key="run.run_id"
          class="rounded-card border border-edge bg-surface-2 px-4 py-3"
          data-testid="tenant-rule-run-item"
        >
          <p class="text-xs text-primary">
            规则运行 {{ shorten(run.run_id) }} · {{ run.event_type }} ·
            <span class="text-brand">{{ run.label }}</span>
          </p>
          <p class="mt-1 text-xs text-muted break-all">{{ run.detail || '无详情' }}</p>
        </li>
      </ul>
    </div>
  </section>
</template>
