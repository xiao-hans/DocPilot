<script setup lang="ts">
/**
 * 下载按钮（第三幕）：Excel 直链下载。
 * <a href="/api/jobs/{id}/excel" download>，由浏览器处理 Content-Disposition，
 * 不经过 fetch blob。
 */

import { excelDownloadUrl } from '../api/client'

defineProps<{ jobId: string }>()
</script>

<template>
  <a
    :href="excelDownloadUrl(jobId)"
    download
    class="inline-flex items-center gap-2 rounded-control bg-brand px-5 py-2.5 font-display text-sm font-semibold text-on-brand transition-colors duration-fast hover:bg-brand-hover"
  >
    <svg
      class="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M12 3v12" />
      <path d="m7 10.5 5 4.5 5-4.5" />
      <path d="M3.5 15.5v3A2.5 2.5 0 0 0 6 21h12a2.5 2.5 0 0 0 2.5-2.5v-3" />
    </svg>
    下载 Excel
  </a>
</template>