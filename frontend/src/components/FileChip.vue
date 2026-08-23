<script setup lang="ts">
/**
 * 文件 chip：文件名（等宽）+ 大小（等宽）+ 可聚焦的移除按钮。
 */

defineProps<{ name: string; sizeLabel: string }>()
defineEmits<{ remove: [] }>()
</script>

<template>
  <div class="flex items-center gap-3 rounded-control border border-edge bg-surface px-3 py-2">
    <svg
      class="h-4 w-4 shrink-0 text-brand"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M13 2.5H6a1.5 1.5 0 0 0-1.5 1.5v16A1.5 1.5 0 0 0 6 21.5h12a1.5 1.5 0 0 0 1.5-1.5V8Z" />
      <path d="M13 2.5V8h5.5" />
    </svg>
    <span class="min-w-0 flex-1 truncate font-mono text-sm text-primary" :title="name">{{
      name
    }}</span>
    <span class="shrink-0 font-mono text-xs text-muted tabular-nums">{{ sizeLabel }}</span>
    <button
      type="button"
      class="flex h-6 w-6 shrink-0 items-center justify-center rounded-control text-muted transition-colors duration-fast hover:bg-surface-2 hover:text-primary"
      :aria-label="`移除文件 ${name}`"
      @click="$emit('remove')"
    >
      <svg
        class="h-3.5 w-3.5"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        aria-hidden="true"
      >
        <path d="M6 6l12 12M18 6 6 18" />
      </svg>
    </button>
  </div>
</template>