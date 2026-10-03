import { defineConfig } from '@playwright/test'

/**
 * DocAI 前端端到端测试配置。
 * - 本地头：系统 Chrome（Playwright 未下载自带 chromium，用系统 Chrome 的稳定路径）
 * - 服务生命周期：globalSetup 启动一次性后端(8123)/前端 dev(5173)，globalTeardown 清理，进程零残留
 * - 单 worker：共享后端，且场景间相互独立（每个测试自己的 job 与文件）
 * - retries=1 + on-first-retry trace：flake 有痕迹，「retry 才过 = flake」度量保留
 */
export default defineConfig({
  testDir: './tests',
  globalSetup: './tests/global-setup.ts',
  globalTeardown: './tests/global-teardown.ts',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:5173',
    headless: true,
    launchOptions: {
      // 系统 Chrome 稳定路径；forward slashes 在 Windows 下同样合法
      executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
      args: ['--disable-dev-shm-usage'],
    },
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
})