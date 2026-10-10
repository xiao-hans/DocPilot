import { expect, test, type Page } from '@playwright/test'
import {
  ADMIN_EMAIL,
  INVOICE_NUMBER,
  REVISED_SELLER,
 SELLER,
  TENANT_NAME,
  badDocx,
  downloadLink,
  downloadedSize,
  dropZone,
  expectComputedStyle,
  BRAND_RGB,
  fileInput,
  invoicePdf,
  openView,
  rowCell,
  signIn,
  startProcessing,
  submitButton,
  waitForSuccess,
} from './helpers'

/**
 * 阶段 G5 端到端用例（TC-E2E-001 与各管理面）。
 *
 * 环境由 `global-setup.ts` 提供：一次性后端（独立 SQLite + 独立上传/输出目录）与前端 dev server，
 * 因此这里所有写操作都落在跑完即弃的数据目录里，不触碰开发数据。
 * 单 worker 顺序执行：第一个用例完成一次性平台初始化，其余走正常登录。
 */

async function activeTenantId(page: Page): Promise<string> {
  const me = await page.request.get('/api/v1/auth/me')
  expect(me.ok(), `登录态应可用：${me.status()}`).toBe(true)
  const body = await me.json()
  return body.tenants[0].id
}

test.describe('DocAI 端到端', () => {
  test('首次部署：平台初始化后进入空白工作区', async ({ page }) => {
    await signIn(page)
    await expect(page.locator('#active-workspace')).toContainText(TENANT_NAME)
    await expect(page.getByText(ADMIN_EMAIL)).toBeVisible()
    await expect(dropZone(page)).toBeVisible()
    await expect(submitButton(page)).toBeDisabled()
    await expectComputedStyle(page, submitButton(page), 'background-color', BRAND_RGB)
  })

  test('上传发票：处理完成后结果表给出真实提取值', async ({ page }) => {
    await signIn(page)
    await startProcessing(page)
    await waitForSuccess(page)

    await expect(page.getByText('1 个文件 · 6 个字段')).toBeVisible()
    await expect(rowCell(page, 0)).toHaveText('invoice_240615.pdf')
    await expect(rowCell(page, 1)).toHaveText(INVOICE_NUMBER)
    await expect(rowCell(page, 2)).toHaveText('2024-06-15')
    await expect(rowCell(page, 3)).toHaveText(SELLER)
    await expect(rowCell(page, 5)).toHaveText('1280')

    const href = await downloadLink(page).getAttribute('href')
    expect(href).toContain('/excel')
  })

  test('复核：保存修订并确认，结果读路径立即反映修订值', async ({ page }) => {
    await signIn(page)
    await startProcessing(page)
    await waitForSuccess(page)

    // 任务 ID 从复核台真正加载的那条结果里取，而不是从下载链接猜：队列里可能留着上一个用例的同名文件。
    const loadedJobIds: string[] = []
    page.on('response', (response) => {
      const path = new URL(response.url()).pathname
      if (!/\/results\/[^/]+$/.test(path) || !response.ok()) return
      response
        .json()
        .then((body) => body?.job_id && loadedJobIds.push(String(body.job_id)))
        .catch(() => undefined)
    })

    await openView(page, '复核')
    // 进入复核台就会自动打开队列首条并重新写入草稿：必须等这条 GET 落地再改值，
    // 否则填进去的修订会被随后的草稿初始化覆盖，"保存修订"始终是禁用态。
    await expect
      .poll(() => loadedJobIds.length, { timeout: 10_000 })
      .toBeGreaterThan(0)
    await expect(page.getByText('机器结果 · v1').first()).toBeVisible()

    await page.locator('input[aria-label="销售方名称"]').fill(REVISED_SELLER)
    await expect(page.getByRole('button', { name: '保存修订' })).toBeEnabled()
    await page.getByRole('button', { name: '保存修订' }).click()
    await expect(page.getByText('已修订 · v2').first()).toBeVisible()

    await page.getByRole('button', { name: '确认结果' }).click()
    await expect(page.getByRole('button', { name: /已确认 · v2/ }).first()).toBeVisible()

    // 关键断言：/result 与 /export 走的是规范化结果，改过的字段不能退回机器值（阶段 G4）。
    const jobId = loadedJobIds[0]!
    const tenant = await activeTenantId(page)
    const result = await (
      await page.request.get(`/api/v1/tenants/${tenant}/jobs/${jobId}/result`)
    ).json()
    expect(result.result[0].values['销售方名称']).toBe(REVISED_SELLER)
    expect(result.result[0].values['发票号码']).toBe(INVOICE_NUMBER)

    const csv = await page.request.get(
      `/api/v1/tenants/${tenant}/jobs/${jobId}/export?format=csv`,
    )
    expect(csv.ok()).toBe(true)
    expect(await csv.text()).toContain(REVISED_SELLER)
  })

  test('文档库：按修订后的字段值检索并保存标注', async ({ page }) => {
    await signIn(page)
    await openView(page, '文档库')

    await page.locator('#library-q').fill(REVISED_SELLER)
    await page.locator('#library-kind').selectOption('field')
    await page.getByRole('button', { name: '检索' }).click()
    await expect(page.getByText(/[1-9]\d* 条命中/).first()).toBeVisible()
    await expect(page.getByText('invoice_240615.pdf').first()).toBeVisible()

    const labelInput = page.locator('input[id^="labels-"]').first()
    await labelInput.fill('E2E 复核样本')
    await page.getByRole('button', { name: '保存' }).first().click()
    await expect(page.getByText('已更新标签').first()).toBeVisible()

    await page.locator('#library-q').fill('不存在的关键词XYZ')
    await page.getByRole('button', { name: '检索' }).click()
    await expect(page.getByText('0 条命中').first()).toBeVisible()
  })

  test('模板管理：新建草稿、发布版本并复制', async ({ page }, testInfo) => {
    await signIn(page)
    await openView(page, '模板')

    // 模板名在工作空间内唯一，重试用同一个名字会撞上上一次尝试留下的草稿。
    const templateName = testInfo.retry ? `E2E 费用单 · 重试 ${testInfo.retry}` : 'E2E 费用单'
    await page.getByRole('button', { name: '新建模板' }).click()
    await page.getByPlaceholder('如：增值税专用发票').fill(templateName)
    const nameInputs = page.locator('input[placeholder="字段名"]')
    const descInputs = page.locator('input[placeholder^="说明："]')
    await nameInputs.nth(0).fill('报销单号')
    await descInputs.nth(0).fill('费用单右上角的报销单号')
    await page.getByRole('button', { name: '加一个字段' }).click()
    await nameInputs.nth(1).fill('金额')
    await descInputs.nth(1).fill('合计金额，数字')
    await page.getByRole('button', { name: '创建草稿' }).click()
    await expect(page.getByText(`模板「${templateName}」已创建为草稿`).first()).toBeVisible()

    // 创建后自动选中：详情区的模板名称输入框回填同一个名字，说明确实切到了这条模板。
    await expect(page.getByRole('textbox', { name: '模板名称' })).toHaveValue(templateName)

    await page.getByRole('button', { name: '发布新版本' }).click()
    await expect(page.getByText('已发布为新版本，之后任务可绑定该版本').first()).toBeVisible()
    await expect(page.getByText(/已发布版本（1）/).first()).toBeVisible()

    await page.getByRole('button', { name: '复制为草稿' }).click()
    await expect(page.getByText('已复制为新的草稿模板').first()).toBeVisible()
  })

  test('团队：生成一次性邀请令牌并可撤回', async ({ page }) => {
    await signIn(page)
    await openView(page, '团队')

    await expect(page.getByRole('heading', { name: '成员与邀请' })).toBeVisible()
    await expect(page.getByLabel('团队管理').getByText(ADMIN_EMAIL).first()).toBeVisible()

    await page.locator('#invite-email').fill('e2e-guest@example.com')
    await page.locator('#invite-role').selectOption('reviewer')
    await page.getByRole('button', { name: '生成邀请' }).click()
    await expect(page.getByText('邀请令牌（只显示这一次）')).toBeVisible()
    await expect(page.getByTestId('invite-token')).toHaveText(/[A-Za-z0-9_-]{40,}/)

    await page.getByRole('button', { name: '我已转交，关闭' }).click()
    await expect(page.getByText('邀请令牌（只显示这一次）')).toBeHidden()

    const pendingRow = page.getByRole('listitem').filter({ hasText: 'e2e-guest@example.com' }).first()
    await expect(pendingRow).toBeVisible()
    await pendingRow.getByRole('button', { name: '撤回' }).click()
    await expect(
      page.getByRole('listitem').filter({ hasText: 'e2e-guest@example.com' }).first(),
    ).toContainText('已撤回')
  })

  test('事件规则：创建、消费事件并暂停', async ({ page }) => {
    await signIn(page)
    await openView(page, '自动化')

    await expect(page.getByRole('heading', { name: '事件规则' })).toBeVisible()
    await page.getByPlaceholder('规则名称').fill('E2E 给已处理文档打标')
    await page.locator('select[aria-label="订阅事件"]').selectOption('job.succeeded')
    await page.locator('select[aria-label="动作类型"]').selectOption('tag_document')
    await page.getByPlaceholder('标签名').fill('E2E 自动标签')
    await page.getByTestId('create-rule').click()
    await expect(page.getByText('规则「E2E 给已处理文档打标」已创建并启用').first()).toBeVisible()

    await page.getByTestId('dispatch-events').click()
    await expect(page.getByText(/消费 \d+ 条事件：执行 \d+ · 跳过 \d+ · 失败 \d+/).first()).toBeVisible()
    await expect(page.getByText(/最近运行（[1-9]\d*）/).first()).toBeVisible()

    const pause = page.locator('[data-testid^="toggle-rule-"]').first()
    await expect(pause).toHaveText('暂停')
    await pause.click()
    await expect(page.locator('[data-testid^="toggle-rule-"]').first()).toHaveText('启用')
  })

  test('拒绝非支持格式：客户端校验拦截，不发上传请求', async ({ page }) => {
    await signIn(page)
    let attempted = false
    await page.route('**/api/v1/tenants/*/jobs*', (route) => {
      if (route.request().method() === 'POST') attempted = true
      return route.continue()
    })
    await fileInput(page).setInputFiles(badDocx)
    await expect(page.getByRole('alert').first()).toContainText(/不支持|格式/)
    await expect(submitButton(page)).toBeDisabled()
    expect(attempted).toBe(false)
  })

  test('键盘可达：拖拽区可聚焦并用 Enter 打开文件选择', async ({ page }) => {
    await signIn(page)
    const chooser = page.waitForEvent('filechooser')
    await dropZone(page).press('Enter')
    const picker = await chooser
    expect(picker).not.toBeNull()
  })

  test('运营：平台区块只有计数与代号，本租户区块给原文', async ({ page }) => {
    await signIn(page)
    await openView(page, '运营')

    await expect(page.getByRole('heading', { name: '用量与失败' })).toBeVisible()
    await expect(page.getByTestId('platform-totals')).toContainText(/个租户 · 文档 \d+/)
    await expect(page.getByTestId('platform-tenant-row').first()).toBeVisible()
    await expect(page.getByTestId('tenant-usage-summary')).toContainText(/文档 \d+ · 版本 \d+/)

    // 平台区块跨租户，后端只给计数与代号：这里断言它确实没把文件名与错误原文带进来。
    const platformUsage = page.getByLabel('平台用量').textContent()
    const platformFailures = page.getByLabel('平台失败分布').textContent()
    expect(await platformUsage).not.toMatch(/\.pdf/i)
    expect(await platformFailures).not.toMatch(/\.pdf/i)

    // 失败分布要么有代号行，要么明确说没有失败；两种都算渲染成功，缺渲染才算失败。
    const hasCodes = await page.getByTestId('platform-failure-code').first().isVisible()
    if (!hasCodes) {
      await expect(page.getByText('窗口内没有失败任务')).toBeVisible()
    }

    await expect(page.getByRole('alert')).toHaveCount(0)

    // 换窗口要真的重取数据，而不是只改下拉框的文字。
    await page.selectOption('#ops-window', '7')
    await expect(page.getByTestId('platform-failure-summary')).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })

  test('运营：处理失败的文档在明细里能看到错误原文', async ({ page }) => {
    await signIn(page)

    // 扩展名合法但内容不是 PDF：上传放行、处理判失败，正是失败明细要呈现的那一类。
    await fileInput(page).setInputFiles({
      name: 'broken.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('\u0000\u0001\u0002 this is not a pdf at all'),
    })
    await submitButton(page).click()
    await expect(page.getByText('处理失败', { exact: true })).toBeVisible({ timeout: 60_000 })

    await openView(page, '运营')
    await expect(page.getByTestId('tenant-failure-summary')).toContainText('失败任务 1 条')

    // 明细项给的是任务号、状态、尝试次数、错误代号与原文；文件名不在其中（对象键不含客户
    // 文件名，这是 G3 定下的形状），所以这里断言"有一行、且原文非空"而不是断言具体措辞。
    const item = page.getByTestId('tenant-failure-item').first()
    await expect(item).toBeVisible()
    await expect(item).toContainText('尝试')
    await expect(item).toContainText(/未分类错误|无法读取|超时|未提取到结果|无错误信息记录/)
    const errorLine = item.locator('p').nth(1)
    await expect(errorLine).not.toHaveText('')
  })

  test('Excel 下载：文件名与内容长度符合预期', async ({ page }) => {
    await signIn(page)
    await startProcessing(page)
    await waitForSuccess(page)
    const download = await Promise.all([
      page.waitForEvent('download'),
      downloadLink(page).click(),
    ]).then(([event]) => event)
    expect(download.suggestedFilename()).toMatch(/\.xlsx$/)
    expect(downloadedSize(await download.path())).toBeGreaterThan(4096)
  })

  /**
   * 维护轮 M6：结果幕的"再处理一份"入口（`docs/06 §5.29`）。
   *
   * 缺口是 M5 修配额用例时撞出来的：上传成功后界面停在结果幕，只有"下载 Excel"，
   * 要开新任务只能刷新页面。这条用例覆盖整条往返——成功后点按钮回到上传幕，
   * 并且**真的能再传一份**（只断言"回到了上传幕"证明不了新任务能创建成功，
   * 场景机切回来但状态没清干净的话，第二次上传会静默失败）。
   */
  test('结果幕：点"再处理一份"回到上传幕，并能再传一份', async ({ page }) => {
    await signIn(page)
    await startProcessing(page)
    await waitForSuccess(page)

    // 结果幕必须有这个入口；缺了它用户只能刷新页面
    const again = page.getByRole('button', { name: '再处理一份' })
    await expect(again).toBeVisible()
    await again.click()

    // 回到第一幕：上传卡片重新出现，结果表消失
    await expect(dropZone(page)).toBeVisible()
    await expect(page.getByText('处理成功', { exact: true })).toBeHidden()

    // 真正再跑一份：这条才是"入口可用"的判据
    await startProcessing(page)
    await waitForSuccess(page)
    await expect(page.getByRole('button', { name: '再处理一份' })).toBeVisible()
  })
})
