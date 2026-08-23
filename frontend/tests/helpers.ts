import type { Locator, Page } from '@playwright/test'
import { expect } from '@playwright/test'
import { existsSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/** 测试资产路径（由 global-setup 生成）。 */
export const tmpDir = path.join(path.dirname(fileURLToPath(import.meta.url)), '.tmp')
export const invoicePdf = path.join(tmpDir, 'invoice_240615.pdf')
export const badDocx = path.join(tmpDir, 'bad.docx')

export const BRAND = '#312C85' // minimal secondary 藏青
export const BRAND_RGB = 'rgb(49, 44, 133)'
export const CANVAS_RGB = 'rgb(244, 244, 241)' // minimal surface

export const INVOICE_NUMBER = '01100200011100045239'

/** 6 个默认提取字段（与 INVOICE_FIELDS 名称一致，用于合成 job 对象）。 */
export const DEFAULT_FIELDS = [
  { name: '发票号码', type: 'text', description: '发票右上角的发票号码（20 位数字）' },
  { name: '开票日期', type: 'date', description: '开票日期 YYYY-MM-DD' },
  { name: '销售方名称', type: 'text', description: '销售方全称' },
  { name: '购买方名称', type: 'text', description: '购买方全称' },
  { name: '价税合计', type: 'number', description: '含税金额（元）' },
  { name: '税额', type: 'number', description: '合计税额（元）' },
] as const

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

/** 上传单个文件（通过隐藏 file input。）。 */
export async function uploadFile(page: Page, fixture: string): Promise<void> {
  if (!existsSync(fixture)) throw new Error(`fixture missing: ${fixture}`)
  await fileInput(page).setInputFiles(fixture)
}

/** 选择文件 → 点击「开始AI分析」，随后场景切到 processing。 */
export async function startProcessing(page: Page, fixture: string = invoicePdf): Promise<void> {
  await uploadFile(page, fixture)
  await submitButton(page).click()
}

/** 等最终结果幕（轮询驱动，含 650ms 成功停留；30s 上限覆盖 60 次轮询的余地）。 */
export async function waitForSuccess(page: Page): Promise<void> {
  await expect(page.getByText('处理成功', { exact: true })).toBeVisible({ timeout: 30_000 })
}

/** 读某个元素的 computed style 属性。 */
export async function computedStyle(page: Page, locator: Locator, prop: string): Promise<string> {
  return locator.evaluate(
    (el, p) => window.getComputedStyle(el).getPropertyValue(p).trim(),
    prop,
  )
}

/** 断言 computed style 等于期望值，失败提供上下文。 */
export async function expectComputedStyle(
  page: Page,
  locator: Locator,
  prop: string,
  expected: string,
): Promise<void> {
  const actual = await computedStyle(page, locator, prop)
  expect(actual, `${prop} should be ${expected}, got ${actual}`).toBe(expected)
}

/** 下载文件大小（存在性 + >0）。 */
export function downloadedSize(filePath: string | null): number {
  expect(filePath, 'download must have a path').not.toBeNull()
  return statSync(filePath!).size
}