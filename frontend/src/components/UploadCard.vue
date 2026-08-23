<script setup lang="ts">
/**
 * 上传卡片（第一幕）：
 *  - 拖拽区：虚线边框 → 拖入时金色实线；可 Tab 聚焦（Enter/Space 打开选择器）
 *  - 文件 chips 可移除；客户端先行校验（扩展名/数量/大小）
 *  - 错误条（role=alert）；"开始AI分析"金色主按钮
 */

import { ref } from 'vue'
import FileChip from './FileChip.vue'
import { createJob, INVOICE_FIELDS } from '../api/client'
import { UPLOAD_LIMITS, useFileSelection } from '../composables/useFileSelection'

const emit = defineEmits<{ 'job-created': [jobId: string, fileCount: number] }>()

const {
  files,
  errors,
  isDragging,
  canSubmit,
  accept,
  removeFile,
  clearErrors,
  onDragEnter,
  onDragOver,
  onDragLeave,
  onDrop,
} = useFileSelection()

const inputRef = ref<HTMLInputElement | null>(null)
const submitting = ref(false)

function pick() {
  clearErrors()
  inputRef.value?.click()
}

function onInputChange(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files) accept(input.files)
  input.value = '' // 允许重复选择同一文件时再次触发 change
}

async function onSubmit() {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  clearErrors()
  try {
    const resp = await createJob(
      files.value.map((f) => f.file),
      INVOICE_FIELDS,
    )
    emit('job-created', resp.job_id, files.value.length)
  } catch (err) {
    errors.value = [err instanceof Error ? err.message : '提交失败，请重试']
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="w-full" aria-label="文件上传">
    <!-- 错误条 -->
    <Transition name="fade-fast">
      <div
        v-if="errors.length > 0"
        role="alert"
        class="mb-4 flex items-start gap-2 rounded-card border border-danger bg-surface px-4 py-3"
      >
        <svg
          class="mt-0.5 h-4 w-4 shrink-0 text-danger"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7.5v6" />
          <path d="M12 16.8v.2" />
        </svg>
        <ul class="min-w-0 flex-1 space-y-1 text-sm text-primary">
          <li v-for="(err, i) in errors" :key="i">{{ err }}</li>
        </ul>
      </div>
    </Transition>

    <!-- 拖拽区：虚线边框 → 拖入时金色实线 -->
    <div
      class="rounded-card border-2 border-dashed bg-surface px-6 py-12 text-center transition-colors duration-fast"
      :class="isDragging ? 'border-solid border-brand' : 'border-edge'"
      role="button"
      tabindex="0"
      :aria-label="`选择文件或拖拽文件到此处。支持 ${UPLOAD_LIMITS.extensions.join('/').toUpperCase()}，单文件不超过 ${UPLOAD_LIMITS.maxFileMB}MB，最多 ${UPLOAD_LIMITS.maxFiles} 个`"
      @click="pick"
      @keydown.enter="pick"
      @keydown.space.prevent="pick"
      @dragenter="onDragEnter"
      @dragover="onDragOver"
      @dragleave="onDragLeave"
      @drop="onDrop"
    >
      <!-- 隐藏文件输入：焦点由拖拽区承担，输入本身不进 Tab 顺序 -->
      <input
        ref="inputRef"
        type="file"
        class="sr-only"
        tabindex="-1"
        multiple
        accept=".pdf,.png,.jpg,.jpeg"
        @change="onInputChange"
      />
      <svg
        class="mx-auto h-10 w-10 text-brand"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M12 15V3" />
        <path d="m7 7.5 5-4.5 5 4.5" />
        <path d="M3.5 15.5v3A2.5 2.5 0 0 0 6 21h12a2.5 2.5 0 0 0 2.5-2.5v-3" />
      </svg>
      <p class="mt-4 font-display text-base font-medium text-primary">拖拽文件到此处，或点击选择</p>
      <p class="mt-2 text-xs text-secondary">PDF / 图片 → 结构化数据 → Excel</p>
      <p class="mt-1 text-xs text-muted">支持 PDF / PNG / JPG / JPEG · 单文件 ≤ 20MB · 最多 10 个</p>
    </div>

    <!-- 文件 chips -->
    <ul v-if="files.length > 0" class="mt-4 space-y-2">
      <li v-for="(item, idx) in files" :key="`${item.file.name}-${item.file.size}`">
        <FileChip :name="item.file.name" :size-label="item.sizeLabel" @remove="removeFile(idx)" />
      </li>
    </ul>

    <!-- 提交区 -->
    <div class="mt-6 flex items-center justify-between gap-4">
      <p v-if="files.length > 0" class="font-mono text-xs text-muted tabular-nums">
        已选择 {{ files.length }} 个文件
      </p>
      <p v-else class="text-xs text-muted">默认按发票模板提取 6 个字段</p>
      <button
        type="button"
        class="rounded-control bg-brand px-6 py-3 font-display text-sm font-semibold text-on-brand transition-colors duration-fast hover:bg-brand-hover disabled:pointer-events-none disabled:opacity-40"
        :disabled="!canSubmit || submitting"
        @click="onSubmit"
      >
        {{ submitting ? '提交中…' : '开始AI分析' }}
      </button>
    </div>
  </section>
</template>