// 为 UI Designer 审查生成三幕真实截图（运行：node tests/shoot.mjs）
import { chromium } from "playwright";
import { execSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const shots = path.join(__dirname, "shots");
const pdf = path.join(__dirname, "invoice_e2e.pdf");

// 用 Tester 的生成器产真实发票 PDF
try {
  execSync(
    `"D:/workspace/DocPilot/.venv/Scripts/python.exe" -X utf8 "${path.join(__dirname, "gen_invoice_pdf.py")}" "${pdf}"`,
    { stdio: "ignore" },
  );
} catch {
  execSync(
    `"D:/workspace/DocPilot/.venv/Scripts/python.exe" -X utf8 -c "import pymupdf;d=pymupdf.open();p=d.new_page();p.insert_text((72,72),'发票号码 01100200011100045239 开票日期 2024年06月15日 价税合计 1280.00 税额 73.92');d.save(r'${pdf}');d.close()"`,
    { stdio: "ignore" },
  );
}

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto("http://127.0.0.1:5173/", { waitUntil: "networkidle" });
await page.waitForTimeout(600);
await page.screenshot({ path: path.join(shots, "1_upload.png") });

// 上传 → 处理幕（拦截轮询延迟 800ms 定格）
await page.locator('input[type="file"]').setInputFiles(pdf);
await page.route("**/api/jobs/*", async (route) => {
  await new Promise((r) => setTimeout(r, 800));
  await route.continue();
});
await page.getByRole("button", { name: /开始AI分析/ }).click();
await page.waitForTimeout(500);
await page.screenshot({ path: path.join(shots, "2_processing.png") });

// 结果幕
await page.waitForSelector("text=处理成功", { timeout: 30000 });
await page.waitForTimeout(500);
await page.screenshot({ path: path.join(shots, "3_result.png") });

// 1440 与 390 移动态对比
const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
await mobile.goto("http://127.0.0.1:5173/", { waitUntil: "networkidle" });
await mobile.waitForTimeout(600);
await mobile.screenshot({ path: path.join(shots, "4_mobile_upload.png") });

await browser.close();
console.log("shots done:", shots);
