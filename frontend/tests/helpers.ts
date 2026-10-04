import type { Locator, Page } from '@playwright/test'
import { expect } from '@playwright/test'
import { existsSync, statSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { BOOTSTRAP_TOKEN, READY_MARKER } from './global-setup'

/**
 * 端到端用例的公共动作。
 *
 * 确定性原则：等待一律基于条件（元素/下载对象），不用固定 sleep；一次运行只有一个
 * worker，平台初始化（一次性、不可重入）由第一个用例完成，其余用例走正常登录。
 */

export const tmpDir = path.join(path.dirname(fileURLToPath(import.meta.url)), '.tmp')
export const invoicePdf = path.join(tmpDir, 'invoice_240615.pdf')
export const badDocx = path.join(tmpDir, 'bad.docx')

export const BRAND_RGB = 'rgb(49, 44, 133)' // minimal secondary 藏青
export const CANVAS_RGB = 'rgb(244, 244, 241)' // minimal surface

export const ADMIN_EMAIL = 'e2e-admin@example.com'
export const ADMIN_PASSWORD = 'e2e-password-1234'
export const TENANT_NAME = '端到端工作空间'

export const INVOICE_NUMBER = '01100200011100045239'
export const SELLER = '北京恒达科技有限公司'
export const REVISED_SELLER = '已核对的北京恒达'

export const fileInput = (page: Page): Locator => page.locator('input[type="file"]')
export const dropZone = (page: Page): Locator =>
  page.getByRole('button', { name: /选择文件或拖拽文件到此处/ })
export const submitButton = (page: Page): Locator =>
  page.getByRole('button', { name: '开始AI分析' })
export const progressRegion = (page: Page): Locator =>
  page.getByRole('region', { name: '处理进度' })
export const downloadLink = (page: Page): Locator =>
  page.getByRole('link', { name: '下载 Excel' })
export const firstRow = (page: Page): Locator => page.locator('tbody tr').first()
export const rowCell = (page: Page, index: number): Locator => firstRow(page).locator('td').nth(index)

/**
 * 进入工作区：首个用例完成一次性平台初始化，之后的用例走登录（同一后端、独立浏览器上下文）。
 *
 * "已初始化"这个事实写进 `data/e2e/`（每次运行都会被清空重建），而不是只放在模块变量里：
 * 用例硬超时会让 Playwright 重启 worker，模块状态随之丢失，届时后续用例会重跑一次
 * 不可重入的初始化并撞上「系统已初始化」。
 */
export async function signIn(page: Page): Promise<void> {
  await page.goto('/')
  const mustBootstrap = !existsSync(READY_MARKER)
  if (mustBootstrap) {
    await page.getByRole('button', { name: '首次部署？初始化平台' }).click()
    await page.locator('#bootstrap-token').fill(BOOTSTRAP_TOKEN)
    await page.locator('#bootstrap-email').fill(ADMIN_EMAIL)
    await page.locator('#bootstrap-password').fill(ADMIN_PASSWORD)
    await page.locator('#bootstrap-tenant').fill(TENANT_NAME)
    await page.getByRole('button', { name: '初始化平台' }).click()
  } else {
    await page.locator('#login-email').fill(ADMIN_EMAIL)
    await page.locator('#login-password').fill(ADMIN_PASSWORD)
    await page.getByRole('button', { name: '登录' }).click()
  }
  await expect(page.getByRole('button', { name: '处理' })).toBeVisible()
  if (mustBootstrap) writeFileSync(READY_MARKER, JSON.stringify({ email: ADMIN_EMAIL }))
}

/** 切换顶部工作区标签；具体断言由各用例自己的定位器负责。 */
export async function openView(page: Page, label: string): Promise<void> {
  await page.getByRole('button', { name: label, exact: true }).first().click()
}

export async function uploadFile(page: Page, fixture: string): Promise<void> {
  if (!existsSync(fixture)) throw new Error(`fixture missing: ${fixture}`)
  await fileInput(page).setInputFiles(fixture)
}

/** 选择文件 → 点击「开始AI分析」，随后场景切到 processing。 */
export async function startProcessing(page: Page, fixture: string = invoicePdf): Promise<void> {
  await uploadFile(page, fixture)
  await submitButton(page).click()
}

/** 等最终结果幕（轮询驱动，含成功后的短暂停留；30s 覆盖 60 次轮询的余地）。 */
export async function waitForSuccess(page: Page): Promise<void> {
  // 队列形态下结果由独立 Worker 产出（派发轮询 + 领取租约 + 执行 + 回写），比进程内基线慢，
  // 30 秒会让用例在环境正确时也随机失败。默认仍是 30 秒，Compose 形态由 setup 抬高。
  await expect(page.getByText('处理成功', { exact: true })).toBeVisible({
    timeout: Number(process.env.E2E_SUCCESS_TIMEOUT_MS ?? 30_000),
  })
}

export async function computedStyle(page: Page, locator: Locator, prop: string): Promise<string> {
  return locator.evaluate((el, p) => window.getComputedStyle(el).getPropertyValue(p).trim(), prop)
}

export async function expectComputedStyle(
  page: Page,
  locator: Locator,
  prop: string,
  expected: string,
): Promise<void> {
  const actual = await computedStyle(page, locator, prop)
  expect(actual, `${prop} should be ${expected}, got ${actual}`).toBe(expected)
}

export function downloadedSize(filePath: string | null): number {
  expect(filePath, 'download must have a path').not.toBeNull()
  return statSync(filePath!).size
}
