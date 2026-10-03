import { spawn, spawnSync } from 'node:child_process'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 全局 setup：为端到端用例准备一套"只属于本次运行"的环境。
 *
 * 1. 生成测试资产（发票 PDF / 非法 docx）
 * 2. 起一次性后端（独立端口 + 独立 SQLite/上传/输出目录，全部落在被忽略版本控制的
 *    `data/e2e/`）与前端 dev server，代理指向该端口
 * 3. 记录 pid，teardown 只清理本次自己启动的进程
 *
 * 这里刻意不"复用已在 :8000 的服务"：被复用的后端连着开发库，端到端跑起来就会往
 * 真实开发数据里写文件与任务。端口被占用时直接失败，让人去处理，而不是退回到危险路径。
 */

const testsDir = path.dirname(fileURLToPath(import.meta.url))
export const tmpDir = path.join(testsDir, '.tmp')
const frontendRoot = path.resolve(testsDir, '..')
const repoRoot = path.resolve(frontendRoot, '..')
const VENV_PY = path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
const VITE_BIN = path.join(frontendRoot, 'node_modules', 'vite', 'bin', 'vite.js')

/** 一次性数据目录：`data/` 已在 .gitignore 内，测试产物不进仓库。 */
export const e2eDataDir = path.join(repoRoot, 'data', 'e2e')
const posixDataDir = e2eDataDir.replace(/\\/g, '/')

export const BACKEND_PORT = Number(process.env.E2E_BACKEND_PORT ?? 8123)
export const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`
export const FRONTEND_ORIGIN = 'http://127.0.0.1:5173'
export const BOOTSTRAP_TOKEN = 'e2e-platform-bootstrap-token'
/** 一次性数据目录内的"平台已初始化"标记：随目录一起重建，因此不会跨运行残留。 */
export const READY_MARKER = path.join(e2eDataDir, 'platform-ready.json')
const SERVERS_FILE = path.join(tmpDir, 'servers.json')

interface StartedServer {
  pid: number
  role: string
  log: string
}

const started: StartedServer[] = []

async function alive(url: string, timeoutMs = 2500): Promise<boolean> {
  try {
    const ctrl = new AbortController()
    const t = setTimeout(() => ctrl.abort(), timeoutMs)
    const res = await fetch(url, { signal: ctrl.signal })
    clearTimeout(t)
    return res.status < 500
  } catch {
    return false
  }
}

async function waitAlive(url: string, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await alive(url)) return true
    await new Promise((r) => setTimeout(r, 500))
  }
  return alive(url)
}

function startServer(
  cmd: string[],
  cwd: string,
  role: string,
  logName: string,
  env: Record<string, string>,
): Promise<number> {
  const logFile = path.join(tmpDir, logName)
  return new Promise<number>((resolve, reject) => {
    const child = spawn(cmd[0], cmd.slice(1), {
      cwd,
      env: { ...process.env, ...env, PYTHONIOENCODING: 'utf-8', FORCE_COLOR: '0' },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const append = (d: unknown) => {
      try {
        writeFileSync(logFile, String(d), { flag: 'a' })
      } catch {
        /* 日志写失败不阻塞启动 */
      }
    }
    child.stdout.on('data', append)
    child.stderr.on('data', append)
    child.on('error', (e) => reject(new Error(`${role} spawn failed: ${e.message}`)))
    child.on('spawn', () => resolve(child.pid!))
    setTimeout(() => reject(new Error(`${role} startup timeout`)), 30_000)
  })
}

function logTail(logFile: string, n = 25): string {
  try {
    const lines = readFileSync(logFile, 'utf8').split(/\r?\n/).filter(Boolean)
    return lines.slice(-n).join('\n')
  } catch {
    return '(no log)'
  }
}

function generateFixtures(): void {
  mkdirSync(tmpDir, { recursive: true })
  const pdf = path.join(tmpDir, 'invoice_240615.pdf')
  const script = path.join(testsDir, 'gen_invoice_pdf.py')
  const gen = spawnSync(VENV_PY, ['-X', 'utf8', script, pdf], { cwd: repoRoot, encoding: 'utf8' })
  if (gen.status !== 0) {
    throw new Error(`PDF generation failed:\n${gen.stdout}\n${gen.stderr}`)
  }
  process.stdout.write(`[setup] ${gen.stdout.trim().split('\n').pop()}\n`)
  writeFileSync(path.join(tmpDir, 'bad.docx'), 'eroteme-not-a-real-docx-for-client-validation')
}

export default async function globalSetup(): Promise<void> {
  generateFixtures()

  // 每次运行都从空目录开始：库、上传对象与导出文件都是本次用例自己的，跑完即弃。
  rmSync(e2eDataDir, { recursive: true, force: true })
  mkdirSync(e2eDataDir, { recursive: true })

  if (await alive(`${BACKEND_ORIGIN}/healthz`)) {
    throw new Error(
      `端到端要求 ${BACKEND_ORIGIN} 空闲：该端口上已有的服务连着开发数据，复用它会把测试写进真实数据`,
    )
  }

  const backendPid = await startServer(
    [
      VENV_PY,
      '-m',
      'uvicorn',
      'app.main:app',
      '--host',
      '127.0.0.1',
      '--port',
      String(BACKEND_PORT),
      '--log-level',
      'warning',
    ],
    repoRoot,
    'backend',
    'backend.log',
    {
      APP_ENV: 'development',
      AUTH_COOKIE_SECURE: 'false',
      EXTRACTOR_MODE: 'mock',
      BOOTSTRAP_TOKEN,
      // 队列保持关闭：端到端跑的是"上传即处理完"的进程内基线，起 Worker 属于另一条链路。
      BROKER_URL: '',
      STORAGE_BACKEND: 'local',
      DATABASE_URL: `sqlite:///${posixDataDir}/e2e.sqlite3`,
      DATA_DIR: posixDataDir,
      UPLOAD_DIR: `${posixDataDir}/uploads`,
      OUTPUT_DIR: `${posixDataDir}/outputs`,
    },
  )
  started.push({ pid: backendPid, role: 'backend', log: path.join(tmpDir, 'backend.log') })
  if (!(await waitAlive(`${BACKEND_ORIGIN}/healthz`, 60_000))) {
    throw new Error(`backend failed to start; log tail:\n${logTail(path.join(tmpDir, 'backend.log'))}`)
  }
  process.stdout.write(`[setup] backend started pid=${backendPid} at ${BACKEND_ORIGIN}\n`)

  const frontendPid = await startServer(
    [process.execPath, VITE_BIN, '--port', '5173', '--strictPort'],
    frontendRoot,
    'frontend',
    'frontend.log',
    { DOCPILOT_API_ORIGIN: BACKEND_ORIGIN },
  )
  started.push({ pid: frontendPid, role: 'frontend', log: path.join(tmpDir, 'frontend.log') })
  if (!(await waitAlive(FRONTEND_ORIGIN, 60_000))) {
    throw new Error(`frontend failed to start; log tail:\n${logTail(path.join(tmpDir, 'frontend.log'))}`)
  }
  process.stdout.write(`[setup] frontend started pid=${frontendPid}\n`)

  writeFileSync(SERVERS_FILE, JSON.stringify({ started }))

  // 首个页面加载会触发 Vite 预构建，先预热一次，避免第一条用例把编译时间算进断言超时。
  const warm = await fetch(`${FRONTEND_ORIGIN}/`)
  if (!(await warm.text()).includes('DocAI')) {
    throw new Error('frontend served unexpected index.html')
  }
  process.stdout.write('[setup] warm request ok\n')
}
