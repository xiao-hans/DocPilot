<script setup lang="ts">
/**
 * 结果预览（第三幕）：表格。
 *  - 首列 filename（等宽）
 *  - 表头 surface-2、细边框、无斑马纹
 *  - 数值列右对齐 + 等宽；空值显示 "—"；空态文案
 */

import type { FieldDefinition, ResultRow } from '../api/client'

const props = defineProps<{ rows: ResultRow[] | null; fields: FieldDefinition[] }>()

function cellText(value: unknown): string {
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function isNumberColumn(field: FieldDefinition): boolean {
  return field.type === 'number'
}
</script>

<template>
  <!-- 空态 -->
  <div
    v-if="!props.rows || props.rows.length === 0"
    class="rounded-card border border-edge bg-surface px-6 py-12 text-center"
  >
    <svg
      class="mx-auto h-8 w-8 text-muted"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <rect x="3.5" y="4.5" width="17" height="15" rx="1.5" />
      <path d="M3.5 9h17" />
      <path d="M8 14h8" />
    </svg>
    <p class="mt-3 text-sm text-muted">暂无结果数据，请检查上传文件内容后重试</p>
  </div>

  <!-- 结果表格 -->
  <div v-else class="overflow-x-auto rounded-card border border-edge bg-surface">
    <table class="w-full border-collapse text-sm">
      <thead>
        <tr class="bg-surface-2">
          <th
            scope="col"
            class="border-b border-edge px-4 py-3 text-left font-display text-xs font-semibold tracking-wide text-primary"
          >
            filename
          </th>
          <th
            v-for="field in props.fields"
            :key="field.name"
            scope="col"
            class="border-b border-edge px-4 py-3 font-display text-xs font-semibold tracking-wide text-primary"
            :class="isNumberColumn(field) ? 'text-right font-mono' : 'text-left'"
          >
            {{ field.name }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, i) in props.rows" :key="i" class="border-b border-edge last:border-b-0">
          <td class="px-4 py-3 align-top font-mono text-xs text-secondary">
            {{ row.file }}
          </td>
          <td
            v-for="field in props.fields"
            :key="field.name"
            class="px-4 py-3 align-top text-primary"
            :class="isNumberColumn(field) ? 'text-right font-mono tabular-nums' : 'text-left'"
          >
            {{ cellText(row.values[field.name]) }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>