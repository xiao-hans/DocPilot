/**
 * TC-E2E-003：完整可访问性矩阵（NFR-007，`docs/06 §5.7`）。
 *
 * 判据四条，逐条落成可失败断言：
 * 1. **读屏可读** —— 每个可见交互控件都有可访问名（`aria-label` / `aria-labelledby` /
 *    `label[for]` / 包裹 label / 文本 / `placeholder` / `title`）。
 * 2. **焦点可见** —— 逐个聚焦后，必须出现 outline 或 box-shadow 之一。
 * 3. **键盘全流程可走** —— 导航按钮可由 Tab 到达、由 Enter 激活；且全页没有
 *    `tabindex` 大于 0 的元素（正 tabindex 会打乱自然序）。
 * 4. **无 `role=alert` 残留** —— 逐个切换视图后，`role=alert` 计数为 0。
 *
 * 为什么写成"全量扫描"而不是几条点断言：`docs/06` 里这条挂着"计划，未自动化"，
 * 而点断言只能证明被点到的那几个控件合格。扫描会把**未命名控件清单**打出来，
 * 新加的控件漏了可访问名时它会直接报出是哪一个。
 *
 * 反向验证方式（本轮做过）：给任一按钮去掉文本与 aria-label，或用 `tabindex="1"`
 * 制造正 tabindex，用例应红且点名该项。
 */

import { expect, test, type Page } from '@playwright/test'
import { openView, signIn } from './helpers'

/** 与 `App.vue` 的 VIEWS 一致；`运营` 只在平台管理员身份下可见，本套件用管理员登录。 */
const VIEWS = ['处理', '复核', '文档库', '模板', '团队', '自动化', 'Webhook', '配额', '运营']

interface Violation {
  kind: string
  detail: string
}

/** 收集页面里可见的交互控件。不可见的（display:none / 零尺寸）不算违规。 */
const COLLECT = `
  () => Array.from(
    document.querySelectorAll('button, input, select, textarea, a[href], [role="button"], [tabindex]')
  ).filter((el) => {
    const type = (el.getAttribute('type') || '').toLowerCase()
    if (type === 'hidden' || type === 'file') return false
    if (el.getAttribute('aria-hidden') === 'true') return false
    return el.getClientRects().length > 0
  })
`

/**
 * 可访问名的近似计算：真实实现按 WAI-ARIA 的算法，这里取与项目相关的那些来源。
 *
 * **算文本时必须排除 `aria-hidden="true"` 的子树。** 读屏器会忽略这些节点，所以
 * "按钮里只有一个 `aria-hidden` 图标"等于**没有名字**；而直接用 `el.textContent`
 * 会把图标字符算成名字，于是扫描器放行这种缺陷。这一点是负向验证照出来的：
 * 把保存按钮的可见文本换成一个 `aria-hidden` 的 ✓ 之后，第一版扫描器仍然报通过。
 */
const ACCESSIBLE_NAME = `
  (el) => {
    const aria = el.getAttribute && el.getAttribute('aria-label')
    if (aria && aria.trim()) return { source: 'aria-label', name: aria.trim() }
    const byId = el.getAttribute && el.getAttribute('aria-labelledby')
    if (byId) {
      const text = byId.split(/\\s+/).map((id) => {
        const ref = document.getElementById(id)
        return ref ? (ref.textContent || '').trim() : ''
      }).join(' ').trim()
      if (text) return { source: 'aria-labelledby', name: text }
    }
    if (el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]')
      if (label) {
        const text = (label.textContent || '').trim()
        if (text) return { source: 'label[for]', name: text }
      }
    }
    const wrap = el.closest && el.closest('label')
    if (wrap) {
      const text = (wrap.textContent || '').trim()
      if (text) return { source: 'label-wrap', name: text }
    }
    const clone = el.cloneNode(true)
    clone.querySelectorAll('[aria-hidden="true"]').forEach((n) => n.remove())
    const text = (clone.textContent || '').trim()
    if (text) return { source: 'text', name: text }
    const ph = el.getAttribute && el.getAttribute('placeholder')
    if (ph && ph.trim()) return { source: 'placeholder', name: ph.trim() }
    const title = el.getAttribute && el.getAttribute('title')
    if (title && title.trim()) return { source: 'title', name: title.trim() }
    return null
  }
`

async function scanNames(page: Page): Promise<Violation[]> {
  return page.evaluate(
    ([collect, nameFn]) => {
      const els = eval(collect!)() as HTMLElement[]
      const name = eval(nameFn!) as (el: HTMLElement) => { source: string; name: string } | null
      const out: { kind: string; detail: string }[] = []
      for (const el of els) {
        if (!name(el)) {
          const tag = el.tagName.toLowerCase()
          const id = el.id ? `#${el.id}` : ''
          const cls = typeof el.className === 'string' ? el.className.slice(0, 48) : ''
          out.push({ kind: 'no-accessible-name', detail: `<${tag}${id}> ${cls}`.trim() })
        }
      }
      return out
    },
    [COLLECT, ACCESSIBLE_NAME],
  )
}

async function scanPositiveTabindex(page: Page): Promise<Violation[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll('[tabindex]'))
      .filter((el) => {
        const v = Number(el.getAttribute('tabindex'))
        return Number.isFinite(v) && v > 0 && el.getClientRects().length > 0
      })
      .map((el) => ({
        kind: 'positive-tabindex',
        detail: `<${el.tagName.toLowerCase()} tabindex="${el.getAttribute('tabindex')}">`,
      })),
  )
}

/**
 * 逐个聚焦可见控件，检查是否出现可见焦点指示（outline 或 box-shadow）。
 *
 * **必须用真实 Tab 键推进，不能用 `el.focus()`。** 全局焦点环挂在 `:focus-visible` 上
 * （`assets/tokens.css`），而 `:focus-visible` 只在**键盘模态**下匹配；程序化聚焦会让
 * 浏览器判为指针模态，于是焦点环不出现、扫描报出一堆假违规。第一版就是这样报了 12 项
 * `focus-not-visible`，其中一半是 `pointer-events` 都不到的按钮——那是度量问题，不是产品问题。
 */
async function scanFocusVisible(page: Page): Promise<Violation[]> {
  const out: Violation[] = []
  await page.locator('body').press('Tab')
  for (let i = 0; i < 40; i += 1) {
    const info = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null
      if (!el || el === document.body) return null
      const s = getComputedStyle(el)
      const outlineVisible = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth || '0') > 0
      const shadowVisible = s.boxShadow !== 'none' && s.boxShadow !== ''
      return {
        outlineVisible,
        shadowVisible,
        desc: `<${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}> ${(el.textContent || '').trim().slice(0, 24)}`,
        isBody: false,
      }
    })
    if (!info) break
    if (!info.outlineVisible && !info.shadowVisible) {
      out.push({ kind: 'focus-not-visible', detail: info.desc })
    }
    await page.keyboard.press('Tab')
  }
  return out
}

async function report(page: Page, label: string, items: Violation[]): Promise<void> {
  const text = items.map((v) => `  [${v.kind}] ${v.detail}`).join('\n')
  expect(items, `${label} 发现 ${items.length} 项可访问性问题:\n${text}`).toEqual([])
}

test.describe('可访问性', () => {
  test('TC-E2E-003 全量扫描：可访问名、焦点可见、正 tabindex、告警残留', async ({ page }) => {
    await signIn(page)

    for (const view of VIEWS) {
      await openView(page, view)
      await report(page, `${view} 视图的交互控件可访问名`, await scanNames(page))
      await report(page, `${view} 视图的正 tabindex`, await scanPositiveTabindex(page))
      // 告警残留：切到这个视图后不应存在任何 role=alert
      expect(await page.getByRole('alert').count(), `${view} 视图残留 role=alert`).toBe(0)
    }

    // 焦点可见：在一个控件密集的视图上逐项验证（全量遍历九个视图耗时且重复度高）
    await openView(page, '处理')
    await report(page, '处理视图的焦点可见性', await scanFocusVisible(page))
  })

  test('TC-E2E-003 键盘全流程：导航可由 Tab 到达、Enter 激活、当前视图可播报', async ({ page }) => {
    await signIn(page)

    // 1) 键盘可达：从页面头部起 Tab，最多 40 次，必须落到导航按钮上
    await page.locator('body').press('Tab')
    let reachedNav = false
    for (let i = 0; i < 40; i += 1) {
      const isNavButton = await page.evaluate(() => {
        const el = document.activeElement
        if (!el) return false
        return el.tagName === 'BUTTON' && el.closest('nav[aria-label="工作区"]') !== null
      })
      if (isNavButton) {
        reachedNav = true
        break
      }
      await page.keyboard.press('Tab')
    }
    expect(reachedNav, '导航按钮无法由 Tab 到达').toBe(true)

    // 2) 键盘可操作：Enter 激活后，激活的那个按钮要变成"当前视图"。
    //
    // 判据不能用标题名等于按钮名：导航标签与面板标题本来就不同名（「自动化」的标题是
    // 「事件规则」、「团队」是「成员与邀请」），而「处理」幕根本没有标题。第一版这样就红了，
    // 是判据错，不是产品错。改成断言激活后 `aria-current` 落到该按钮上——这同时是
    // NFR-007 要的"状态辅助播报"：读屏用户得知道自己在哪个视图。
    const before = await page.evaluate(() => (document.activeElement?.textContent || '').trim())
    await page.keyboard.press('Enter')
    const current = page.locator('nav[aria-label="工作区"] button[aria-current="page"]')
    await expect(current, `Enter 激活「${before}」后没有按钮被标为当前视图`).toHaveCount(1)
    await expect(current).toHaveText(before)

    // 3) 切换视图后，当前标记要跟着走（不能只标第一次）
    await page.locator('body').press('Tab')
    for (let i = 0; i < 40; i += 1) {
      const isNavButton = await page.evaluate(() => {
        const el = document.activeElement
        if (!el) return false
        return (
          el.tagName === 'BUTTON' &&
          el.closest('nav[aria-label="工作区"]') !== null &&
          el.getAttribute('aria-current') === null
        )
      })
      if (isNavButton) break
      await page.keyboard.press('Tab')
    }
    const nextLabel = await page.evaluate(() => (document.activeElement?.textContent || '').trim())
    await page.keyboard.press('Enter')
    await expect(current).toHaveCount(1)
    await expect(current).toHaveText(nextLabel)
    expect(nextLabel, '第二个视图与第一个相同，切换未被验证').not.toBe(before)
  })
})
