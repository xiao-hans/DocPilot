import { spawn, spawnSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 全局 setup：
 * 1. 生成测试资产（发票 PDF / 非法 docx）到 tests/.tmp/
 * 2. 探测后端(8000)与前端(5173)；不在线则启动，记录 pid，teardown 时清理
 * 只 kill 自己启动的进程；若服务已在线则复用并记录 reused=true。
 */

const testsDir = path.dirname(fileURLToPath(import.meta.url))
export const tmpDir = path.join(testsDir, '.tmp')
const frontendRoot = path.resolve(testsDir, '..')
const repoRoot = path.resolve(frontendRoot, '..')
const VENV_PY = path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
const VITE_BIN = path.join(frontendRoot, 'node_modules', 'vite', 'bin', 'vite.js')

const BACKEND = 'http://127.0.0.1:8000'
const FRONTEND = 'http://127.0.0.1:5173'
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

function spawnServer(cmd: string[], cwd: string, role: string, logName: string): number {
  const logFile = path.join(tmpDir, logName)
  const log = BunZeroLog(logFile)
  return new Promise<number>((resolve, reject) => {
    const child = spawn(cmd[0], cmd.slice(1), {
      cwd,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', FORCE_COLOR: '0' },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    child.stdout.on('data', (d) => log(String(d)))
    child.stderr.on('data', (d) => log(String(d)))
    child.on('error', (e) => reject(new Error(`${role} spawn failed: ${e.message}`)))
    child.on('exit', (code) => {
      // 非预期提前退出 → 之后健康探测会失败，并且我们不会 kill 它以清理
    })
    // 等 pid 可用
    child.on('spawn', () => resolve(child.pid!))
    setTimeout(() => reject(new Error(`${role} startup timeout`)), 30_000)
  })
}

/** 简单的同步顺序文件写入器（避免多个事件里 fd 竞争）. */
function BunZeroLog(logFile: string) {
  const chunks: string[] = []
  return (d: string) => {
    chunks.push(d)
    try {
      writeFileSync(logFile, chunks.join(''), { flag: 'a' })
    } catch {
      /* log 失败不阻塞启动 */
    }
  }
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
  const gen = spawnSync(VENV_PY, ['-X', 'utf8', script, pdf], {
    cwd: repoRoot,
    encoding: 'utf8',
  })
  if (gen.status !== 0) {
    throw new Error(`PDF generation failed:\n${gen.stdout}\n${gen.stderr}`)
  }
  process.stdout.write(`[setup] ${gen.stdout.trim()}\n`)
  writeFileSync(path.join(tmpDir, 'bad.docx'), 'eroteme-not-a-real-docx-for-client-validation')
}

export default async function globalSetup(): Promise<void> {
  generateFixtures()

  // ---- 后端 ----
  const backendUp = await alive(BACKEND)
  if (backendUp) {
    process.stdout.write('[setup] backend already healthy at :8000, reusing\n')
  } else {
    const pid = await spawnServer(
      [VENV_PY, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000'],
      repoRoot,
      'backend',
      'backend.log',
    )
    started.push({ pid, role: 'backend', log: path.join(tmpDir, 'backend.log') })
    if (!(await waitAlive(BACKEND, 60_000))) {
      throw new Error(
        `backend failed to start; log tail:\n${logTail(path.join(tmpDir, 'backend.log'))}`,
      )
    }
    process.stdout.write(`[setup] backend started pid=${pid}\n`)
  }

  // ---- 前端 dev ----
  const frontendUp = await alive(FRONTEND)
  if (frontendUp) {
    process.stdout.write('[setup] frontend already healthy at :5173, reusing\n')
  } else {
    const pid = await spawnServer(
      [process.execPath, VITE_BIN, '--port', '5173', '--strictPort'],
      frontendRoot,
      'frontend',
      'frontend.log',
    )
    started.push({ pid, role: 'frontend', log: path.join(tmpDir, 'frontend.log') })
    if (!(await waitAlive(FRONTEND, 60_000))) {
      throw new Error(
        `frontend failed to start; log tail:\n${logTail(path.join(tmpDir, 'frontend.log'))}`,
      )
    }
    process.stdout.write(`[setup] frontend started pid=${pid}\n`)
  }

  if (started.length === 0) {
    process.stdout.write('[setup] both services reused, nothing to clean up\n')
  }
  writeFileSync(
    SERVERS_FILE,
    JSON.stringify({
      started,
      reused: { backend: await alive(BACKEND), frontend: await alive(FRONTEND) },
    }),
  )

  // 真实浏览器预热：首个页面加载要跑 vite optimize 预构建，先探一次
  const warm = await fetch(`${FRONTEND}/`)
  const warmHtml = await warm.text()
  if (!warmHtml.includes('DocAI')) {
    throw new Error('frontend served unexpected index.html')
  }
  process.stdout.write('[setup] warm request ok\n')
}