import { test, expect } from '@playwright/test'
import {
  BRAND_RGB,
  CANVAS_RGB,
  DEFAULT_FIELDS,
  INVOICE_NUMBER,
  badDocx,
  downloadLink,
  downloadedSize,
  dropZone,
  expectComputedStyle,
  invoicePdf,
  progressRegion,
  rowCell,
  startProcessing,
  submitButton,
  uploadFile,
  waitForSuccess,
} from './helpers'

/**
 * DocAI 前端端到端测试（本地起真实服务，系统 Chrome 执行）。
 *
 * 确定性原则：
 * - 等待全部基于条件（元素/事件/下载对象），零 waitForTimeout
 * - 每个场景独立上传自己的文件（job 由后端 uuid 隔离），无共享种子数据
 * - 场景 3 用网络拦截把轮询 GET 显式延后，把 processing 幕「定格」到可断言长度，
 *   不依赖后端处理速度（这仍是真实处理视图，只是首轮轮询被推迟）
 */

test.describe('DocAI e2e', () => {
  test('page loads: brand, upload card, gold CTA button', async ({ page }) => {
    await page.goto('/')

    await expect(page).toHaveTitle(/DocAI/)
    await expect(page.getByText('DocAI', { exact: true })).toBeVisible()
    await expect(dropZone(page)).toBeVisible()
    await expect(page.getByText('支持 PDF / PNG / JPG / JPEG · 单文件 ≤ 20MB · 最多 10 个')).toBeVisible()

    // 品牌主按钮（minimal secondary 藏青）：computed background-color == #312C85
    const btn = submitButton(page)
    await expect(btn).toBeVisible()
    await expectComputedStyle(page, btn, 'background-color', BRAND_RGB)
    const btnText = await btn.textContent()
    expect(btnText?.trim()).toBe('开始AI分析')

    // 浅色主题（minimal surface）：根容器 computed background == #F4F4F1
    await expectComputedStyle(page, page.locator('#app > div'), 'background-color', CANVAS_RGB)
    await expect(page.getByText('默认按发票模板提取 6 个字段')).toBeVisible()
    // 提交按钮初始不可用（无文件）
    await expect(btn).toBeDisabled()
  })

  test('upload real invoice PDF: processing → result with extracted values', async ({ page }) => {
    await page.goto('/')
    await startProcessing(page)

    // 进入第二幕
    await expect(progressRegion(page)).toBeVisible()

    // 轮询驱动，等最终结果（≤30s）
    await waitForSuccess(page)

    // 摘要：处理成功 + N 文件/M 字段
    await expect(page.getByText('1 个文件 · 6 个字段')).toBeVisible()
    await expect(downloadLink(page)).toBeVisible()

    // 首行：filename 正确；发票号码 / 开票日期 / 销售方 / 购买方 / 价税合计 / 税额为真实提取值
    await expect(rowCell(page, 0)).toHaveText('invoice_240615.pdf')
    await expect(rowCell(page, 1)).toHaveText(INVOICE_NUMBER)
    await expect(rowCell(page, 2)).toHaveText('2024-06-15')
    await expect(rowCell(page, 3)).toHaveText('北京恒达科技有限公司')
    await expect(rowCell(page, 4)).toHaveText('上海锐进商贸有限公司')
    // 价税合计顶层 regex 提取 "1,280.00"，coerce_number 整数化 → 1280（表列：filename+6 字段）
    await expect(rowCell(page, 5)).toHaveText('1280')
    await expect(rowCell(page, 6)).toHaveText('73.92')

    // 表头：首列为 filename，数值列右对齐
    const header = page.locator('thead th')
    await expect(header.nth(0)).toHaveText('filename')
    const priceAlign = await rowCell(page, 5).evaluate(
      (el) => window.getComputedStyle(el).textAlign,
    )
    expect(priceAlign).toBe('right')
  })

  test('processing timeline shows the 5 documented steps', async ({ page }) => {
    await page.goto('/')

    // 把本轮询 GET 显式延迟 2s：无论后端多快，首轮轮询返回前 processing 幕
    // 必然在屏（真实处理视图，非合成 dom）。POST /api/jobs 不受影响。
    await page.route('**/api/jobs/*', async (route) => {
      if (route.request().method() === 'POST') {
        await route.continue()
        return
      }
      await new Promise((r) => setTimeout(r, 2000))
      await route.continue()
    })

    await startProcessing(page)

    const progress = progressRegion(page)
    await expect(progress).toBeVisible({ timeout: 10_000 })

    // 5 步文案齐全（静态渲染，任意进度下都存在）
    for (const label of [
      '文件上传完成',
      '文档解析完成',
      'OCR 文字识别完成',
      'AI 正在提取字段',
      'Excel 生成完成',
    ]) {
      await expect(progress.getByText(label, { exact: true })).toBeVisible()
    }

    // 状态性断言：首步已 done（金色 ✓）+ 恰好一个 active 步骤带旋转 loader
    await expect(progress.locator('.step-check-pop').first()).toBeVisible()
    const spinners = progress.getByRole('status')
    await expect(spinners.first()).toBeVisible()
    expect(await spinners.count()).toBe(1)

    await expect(progress.getByText('文件上传完成', { exact: true })).toHaveCSS(
      'color',
      'rgb(12, 12, 9)',
    )
  })

  test('download Excel: .xlsx suggested name, non-empty file', async ({ page }) => {
    await page.goto('/')
    await startProcessing(page)
    await waitForSuccess(page)

    const downloadPromise = page.waitForEvent('download')
    await downloadLink(page).click()
    const download = await downloadPromise

    expect(download.suggestedFilename()).toMatch(/\.xlsx$/)
    const filePath = await download.path()
    expect(downloadedSize(filePath)).toBeGreaterThan(0)
  })

  test('rejects .docx client-side without any POST /api/jobs', async ({ page }) => {
    await page.goto('/')

    const postUrls: string[] = []
    page.on('request', (req) => {
      if (req.method() === 'POST' && req.url().includes('/api/jobs')) postUrls.push(req.url())
    })

    await uploadFile(page, badDocx)

    // 错误条（role=alert）出现且明确指向不被支持的类型
    const alert = page.getByRole('alert')
    await expect(alert).toBeVisible()
    await expect(alert).toContainText('不支持的文件类型：bad.docx')
    await expect(alert).toContainText('仅支持 PDF / PNG / JPG / JPEG')

    // 非法文件不进列表：唯一错误条目、无 chips、计数为 0、按钮禁用 → 请求不可能发出
    await expect(alert.locator('li')).toHaveCount(1)
    await expect(page.locator('ul.mt-4')).toHaveCount(0)
    await expect(page.getByText(/已选择/)).toBeHidden()
    await expect(submitButton(page)).toBeDisabled()

    // 截获监听里必须没有任何 POST /api/jobs
    expect(postUrls, 'invalid file must not reach backend').toHaveLength(0)
  })

  test('keyboard a11y: Tab focuses dropzone, Enter opens file chooser', async ({ page }) => {
    await page.goto('/')

    // 首 Tab 命中全页唯一可聚焦元素：拖拽区（role=button, tabindex=0）
    await page.keyboard.press('Tab')
    await expect(dropZone(page)).toBeFocused()

    // Enter 在键盘路径下打开系统文件选择器
    const fcPromise = page.waitForEvent('filechooser')
    await page.keyboard.press('Enter')
    const fileChooser = await fcPromise
    expect(fileChooser).toBeTruthy()

    // 补齐键盘路径：真正选中发票并走完整链路
    await fileChooser.setFiles(invoicePdf)
    await expect(page.getByText('invoice_240615.pdf')).toBeVisible()
    await expect(page.getByText(/已选择 1 个文件/)).toBeVisible()
  })

  test('renders em-dash for empty field values (intercepted succeeded state)', async ({ page }) => {
    test.info().annotations.push({
      type: 'note',
      description:
        'real backend cannot emit empty values (mock extractor always fills fallbacks); ' +
        'this validates the ResultPreview empty-value contract via an intercepted succeeded job',
    })
    await page.goto('/')

    // 轮询 GET→成功、/result→包含空值的结果行；真实上传 POST 仍打到后端
    await page.route('**/api/jobs/*', async (route) => {
      const req = route.request()
      if (req.method() === 'POST') return route.continue()
      const m = req.url().match(/\/api\/jobs\/([^/]+)\/?$/)
      const jobId = m?.[1] ?? 'intercepted'
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: jobId,
          status: 'succeeded',
          progress: 'done',
          created_at: Date.now(),
          files: [{ name: 'empty-sample.pdf', page_count: 1, chars: 12, ocr_applied: false, error: null }],
          fields: DEFAULT_FIELDS,
          result: null,
          warnings: [],
          error: null,
        }),
      })
    })
    await page.route('**/api/jobs/*/result', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          job_id: 'intercepted',
          status: 'succeeded',
          progress: 'done',
          result: [
            {
              file: 'empty-sample.pdf',
              values: {
                发票号码: '',
                开票日期: '',
                销售方名称: '',
                购买方名称: '',
                价税合计: '',
                税额: '',
              },
              warnings: [],
            },
          ],
          warnings: [],
          error: null,
        }),
      })
    })

    await startProcessing(page)
    await waitForSuccess(page)

    // 空值渲染为 —
    await expect(rowCell(page, 0)).toHaveText('empty-sample.pdf')
    await expect(rowCell(page, 1)).toHaveText('—')
    await expect(rowCell(page, 2)).toHaveText('—')
    await expect(rowCell(page, 5)).toHaveText('—')
    await expect(page.getByText('1 个文件 · 6 个字段')).toBeVisible()
  })
})