<script setup lang="ts">
/**
 * ◆ DocAI 品牌标识：线性 SVG 菱形图标 + 字标。无 emoji、无裸色值（currentColor + 令牌）。
 */
</script>

<template>
  <div class="flex items-center gap-3">
    <svg
      class="h-9 w-9 text-brand"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <!-- 外菱形 -->
      <path d="M12 2.5 21.5 12 12 21.5 2.5 12Z" />
      <!-- 内菱形 -->
      <path d="M12 9.4 14.6 12 12 14.6 9.4 12Z" />
    </svg>
    <div class="leading-tight">
      <p class="font-display text-xl font-semibold tracking-wide text-primary">DocAI</p>
      <p class="mt-0.5 text-xs text-muted">AI 驱动的智能文档处理助手</p>
    </div>
  </div>
</template>