import { expect, test } from '@playwright/test'
import { invoicePdf, openView, signIn, startProcessing, submitButton, uploadFile } from './helpers'

/**
 * 配额管理界面（FR-042，阶段 G22）。
 *
 * 这条用例存在的意义不是"能填数字"，而是证明界面背后那道判定真的在链路上生效：
 * 把文档额度压到 1，随后一次真实上传必须被拒且**原因出现在界面上**。
 * 结束时恢复不限，避免把额度留给后面的用例（套件是串行的，共用同一个后端）。
 */
test.describe('配额', () => {
  test('默认不限 → 设额度 → 上传被拒并给出原因 → 恢复不限', async ({ page }) => {
    await signIn(page)
    await openView(page, '配额')

    await expect(page.getByText('额度由租户管理员设定，默认不限')).toBeVisible()
    const state = page.getByTestId('quota-state')
    await expect(state).toHaveText(/不限/)

    await page.getByLabel(/文档额度/).fill('1')
    const save = page.getByRole('button', { name: '保存额度' })
    await expect(save).toBeEnabled()
    await save.click()
    await expect(page.getByText('额度已保存')).toBeVisible()
    await expect(state).toHaveText(/已设置额度/)
    await expect(page.getByText(/已用文档/)).toBeVisible()

    // 额度已经用满：下一次上传必须在服务端被挡下，并把原因显示出来
    await openView(page, '处理')
    await uploadFile(page, invoicePdf)
    await submitButton(page).click()
    await expect(page.getByText(/配额已满/)).toBeVisible({ timeout: 15_000 })

    // 恢复不限，然后证明同样的上传能走通
    await openView(page, '配额')
    await page.getByRole('button', { name: '恢复不限' }).click()
    await expect(page.getByText('已恢复为不限')).toBeVisible()
    await expect(state).toHaveText(/不限/)

    await openView(page, '处理')
    await startProcessing(page)
  })
})
