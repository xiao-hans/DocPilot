<script setup lang="ts">
/**
 * 处理状态（第二幕）：5 步时间线。
 *  - 仅 active 步有旋转 loader；done 金 ✓（200ms 弹出）；error 红 ✗；pending 灰
 *  - 步骤推进只换颜色不位移；prefers-reduced-motion 下全静态（tokens.css 全局关闭）
 *  - failed/超时 → 对应步骤标红 + 错误详情 + "重新选择文件"（返回上传幕）
 */

import { computed } from 'vue'
import type { JobProgress } from '../api/client'

const STEPS = [
  '文件上传完成',
  '文档解析完成',
  'OCR 文字识别完成',
  'AI 正在提取字段',
  'Excel 生成完成',
] as const

type StepState = 'done' | 'active' | 'error' | 'pending'

const props = defineProps<{
  progress: JobProgress | null
  fileCount: number
  failed: boolean
  error: string
}>()

defineEmits<{ retry: [] }>()

/** 当前进度对应的 active 步骤（1 基）。done → 6 = 全部完成。 */
function activeStepIndex(p: JobProgress | null): number {
  switch (p) {
    case 'parsing':
      return 3
    case 'extracting':
      return 4
    case 'excel':
      return 5
    case 'done':
      return 6
    case 'queued':
    default:
      return 2 // null 或 queued：上传已完成，解析进行中
  }
}

/** 失败时红 ✗ 落在的步骤（按最后观察到的进度推断）。 */
function errorStepIndex(p: JobProgress | null): number {
  switch (p) {
    case 'queued':
      return 2
    case 'parsing':
      return 3
    case 'extracting':
      return 4
    case 'excel':
    case 'done':
    default:
      return 5
  }
}

const stepStates = computed<StepState[]>(() => {
  if (props.failed) {
    const errorStep = errorStepIndex(props.progress)
    return STEPS.map((_, i) => (i + 1 < errorStep ? 'done' : i + 1 === errorStep ? 'error' : 'pending'))
  }
  const active = activeStepIndex(props.progress)
  return STEPS.map((_, i) => (i + 1 < active ? 'done' : i + 1 === active ? 'active' : 'pending'))
})
</script>

<template>
  <section class="w-full rounded-card border border-edge bg-surface p-6 sm:p-8" aria-label="处理进度">
    <p class="mb-5 font-display text-base font-semibold text-primary">
      正在处理 {{ props.fileCount }} 个文件…
    </p>
    <ol class="flex flex-col">
      <li
        v-for="(label, i) in STEPS"
        :key="label"
        class="flex gap-3 pb-6 last:pb-0"
        :aria-current="stepStates[i] === 'active' ? 'step' : undefined"
      >
        <div class="flex flex-col items-center">
          <div class="flex h-6 w-6 items-center justify-center">
            <!-- done：金色 ✓（200ms 弹出） -->
            <span
              v-if="stepStates[i] === 'done'"
              class="step-check-pop flex h-5 w-5 items-center justify-center rounded-full bg-brand"
            >
              <svg
                class="h-3 w-3 text-on-brand"
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
            <!-- active：旋转 loader（页面唯一允许的循环动画） -->
            <span
              v-else-if="stepStates[i] === 'active'"
              class="h-5 w-5 animate-spin rounded-full border-2 border-edge border-t-brand"
              role="status"
              :aria-label="`${label}进行中`"
            ></span>
            <!-- error：红色 ✗ -->
            <span
              v-else-if="stepStates[i] === 'error'"
              class="flex h-5 w-5 items-center justify-center rounded-full border-2 border-danger text-danger"
            >
              <svg
                class="h-3 w-3"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="3"
                stroke-linecap="round"
                aria-hidden="true"
              >
                <path d="M6 6l12 12M18 6 6 18" />
              </svg>
            </span>
            <!-- pending：灰色空心圆 -->
            <span v-else class="h-5 w-5 rounded-full border-2 border-edge"></span>
          </div>
          <!-- 连接线：仅非末步 -->
          <div v-if="i < STEPS.length - 1" class="w-0.5 flex-1 bg-edge"></div>
        </div>
        <p
          class="text-sm pt-1.5"
          :class="{
            'text-primary': stepStates[i] === 'done' || stepStates[i] === 'active',
            'text-muted': stepStates[i] === 'pending',
            'text-danger': stepStates[i] === 'error',
          }"
        >
          {{ label }}
        </p>
      </li>
    </ol>

    <!-- 失败/超时详情 -->
    <div v-if="failed" role="alert" class="mt-6 flex items-start gap-3 rounded-control border border-edge bg-surface-2 px-4 py-3">
      <svg
        class="mt-0.5 h-4 w-4 shrink-0 text-danger"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7.5v6" />
        <path d="M12 16.8v.2" />
      </svg>
      <div class="min-w-0 flex-1">
        <p class="text-sm font-semibold text-danger">处理失败</p>
        <p v-if="error" class="mt-1 text-sm text-secondary">{{ error }}</p>
      </div>
      <button
        type="button"
        class="shrink-0 text-xs text-secondary underline-offset-2 transition-colors duration-fast hover:text-primary hover:underline"
        @click="$emit('retry')"
      >
        重新选择文件
      </button>
    </div>
  </section>
</template>