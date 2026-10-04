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
const COMPOSE_HELPER = path.join(repoRoot, 'scripts', 'e2e_compose_env.py')
export const COMPOSE_MODE = process.env.E2E_COMPOSE === '1'
/** 容器形态：API/派发器/Worker 都跑在 compose 的 app profile 里，主机只起 vite。 */
export const APP_CONTAINER_MODE = process.env.E2E_APP_CONTAINERS === '1'
export const RUN_ID = process.env.E2E_RUN_ID ?? `run-${Date.now().toString(36)}`

interface ComposeEnv {
  RUN: string
  DATABASE_URL: string
  BROKER_URL: string
  QUEUE_NAME: string
  STORAGE_BACKEND: string
  S3_KEY_PREFIX: string
  COMPOSE_ENV_FILE: string
  /** host = 主机跑 alembic；compose = 把未迁移的库留给 migrate 容器，用来证明容器连的是 PostgreSQL。 */
  MIGRATOR: 'host' | 'compose'
}

/** 一次性数据目录：`data/` 已在 .gitignore 内，测试产物不进仓库。 */
export const e2eDataDir = path.join(repoRoot, 'data', 'e2e')
const posixDataDir = e2eDataDir.replace(/\\/g, '/')

export const BACKEND_PORT = Number(process.env.E2E_BACKEND_PORT ?? (APP_CONTAINER_MODE ? 8000 : 8123))
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

/**
 * Compose 形态的环境准备交给 Python 侧脚本：它负责依赖健康检查、按精确名字重建一次性
 * PostgreSQL 库、显式跑一次迁移，并划出本次运行的队列名与对象前缀。
 */
function prepareComposeEnv(): ComposeEnv {
  const args = ['-X', 'utf8', COMPOSE_HELPER, 'prepare', '--run', RUN_ID, '--bootstrap-token', BOOTSTRAP_TOKEN]
  if (APP_CONTAINER_MODE) args.push('--app-containers')
  const res = spawnSync(VENV_PY, args, { cwd: repoRoot, encoding: 'utf8' })
  if (res.status !== 0) {
    throw new Error(`Compose 端到端环境准备失败：\n${res.stdout}\n${res.stderr}`)
  }
  const line = res.stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? ''
  try {
    return JSON.parse(line) as ComposeEnv
  } catch {
    throw new Error(`无法解析 prepare 输出：${line}`)
  }
}

/**
 * 停掉 app profile 的四个服务，并相信脚本的回查结论（它自己会因残留返回非零）。
 *
 * setup 阶段 `required=true`：脏栈上开跑的话，端口守卫和"迁移由容器负责"的判据都会失真。
 */
function takeDownAppContainers(where: string, required: boolean): void {
  const res = spawnSync(VENV_PY, ['-X', 'utf8', COMPOSE_HELPER, 'app-down'], {
    cwd: repoRoot,
    encoding: 'utf8',
    maxBuffer: 16 * 1024 * 1024,
  })
  const out = `${res.stdout ?? ''}${res.stderr ?? ''}`.trim()
  for (const line of out.split(/\r?\n/)) {
    if (line) process.stdout.write(`[${where}] ${line}\n`)
  }
  if (res.status !== 0 && required) {
    throw new Error(`app 容器回收不干净（退出码 ${res.status}），不敢在脏栈上开跑`)
  }
}

/**
 * 起 compose 里的应用侧容器，并把"容器真的连上了这套一次性环境"作为返回前提。
 *
 * 判据在 Python 侧：`migrate` 必须跑完退出、`api/dispatcher/worker` 必须 running、
 * 且 PostgreSQL 的一次性库里必须出现 alembic_version——本次运行没在主机迁移过，
 * 所以那条版本记录只可能由容器写入。少这一层的话，容器回落到镜像里的 SQLite
 * 也照样能起、用例也可能照样过，但那已经不是 Compose 形态了。
 */
function bringUpAppContainers(): void {
  const args = ['-X', 'utf8', COMPOSE_HELPER, 'app-up']
  if (process.env.E2E_SKIP_BUILD === '1') args.push('--no-build')
  process.stdout.write(`[setup] app-up（compose profile: app）${args.includes('--no-build') ? ' [复用已有镜像]' : ''}\n`)
  const res = spawnSync(VENV_PY, args, {
    cwd: repoRoot,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  })
  process.stdout.write(res.stdout ?? '')
  if (res.status !== 0) {
    throw new Error(`应用容器启动失败（退出码 ${res.status}）：\n${res.stderr}`)
  }
}

/**
 * 队列形态的就绪门槛：`/readyz` 必须确认"队列已启用且至少有 1 个消费者"。
 *
 * 只看 `/healthz` 或 `/readyz` 返回 200 不够——没有积压时 0 个消费者也算就绪（G-DEC-09 的
 * 口径），那样的话用例会在一个没人消费的队列上等结果。这里显式要求 consumers >= 1，
 * 否则派发器和 Worker 有一边没起来就会在这里失败，而不是让用例随机超时。
 */
async function waitQueueReady(timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let last = '未尝试'
  while (Date.now() < deadline) {
    try {
      const ctrl = new AbortController()
      const t = setTimeout(() => ctrl.abort(), 3000)
      const res = await fetch(`${BACKEND_ORIGIN}/readyz`, { signal: ctrl.signal })
      clearTimeout(t)
      const body = (await res.json()) as {
        queue?: { enabled?: boolean; consumers?: number | null; pending?: number; stuck?: boolean }
      }
      const q = body.queue
      if (res.status === 200 && q?.enabled === true && typeof q.consumers === 'number' && q.consumers >= 1) {
        process.stdout.write(`[setup] readyz：consumers=${q.consumers} pending=${q.pending ?? 0}\n`)
        return
      }
      last = `status=${res.status} queue=${JSON.stringify(q)}`
    } catch (e) {
      last = String(e)
    }
    await new Promise((r) => setTimeout(r, 1000))
  }
  throw new Error(`/readyz 未在 ${timeoutMs}ms 内确认"队列启用且有消费者"：${last}`)
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
  // 这一步必须排在 prepare 之前——容器形态的 env 文件也落在 data/e2e 里，反过来会把它删掉。
  rmSync(e2eDataDir, { recursive: true, force: true })
  mkdirSync(e2eDataDir, { recursive: true })

  // 容器形态先把上一轮的 app 容器停干净再建库：留着旧容器的话，一是删库会把还在连着的 Worker
  // 掐断，二是下面那条"端口必须空闲"的守卫会被自己的旧容器判成占用。
  if (APP_CONTAINER_MODE) takeDownAppContainers('setup-down', true)

  const compose = COMPOSE_MODE ? prepareComposeEnv() : null
  if (compose) {
    process.stdout.write(
      `[setup] compose mode：run=${compose.RUN} queue=${compose.QUEUE_NAME} prefix=${compose.S3_KEY_PREFIX} migrator=${compose.MIGRATOR}\n`,
    )
    // 结果由独立 Worker 产出，进程内基线的 30 秒窗口不够；用例 worker 在 setup 之后启动，
    // 因此这里改 process.env 能传到 helpers.waitForSuccess。
    process.env.E2E_SUCCESS_TIMEOUT_MS = process.env.E2E_SUCCESS_TIMEOUT_MS ?? '120000'
  }

  if (await alive(`${BACKEND_ORIGIN}/healthz`)) {
    throw new Error(
      `端到端要求 ${BACKEND_ORIGIN} 空闲：该端口上已有的服务连着开发数据，复用它会把测试写进真实数据`,
    )
  }

  if (APP_CONTAINER_MODE) {
    if (!compose) {
      throw new Error('E2E_APP_CONTAINERS=1 需要同时设 E2E_COMPOSE=1：应用容器依赖 compose 依赖栈')
    }
    bringUpAppContainers()
    await waitQueueReady(120_000)
  } else {
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
        ...(compose
          ? {
              BROKER_URL: compose.BROKER_URL,
              QUEUE_NAME: compose.QUEUE_NAME,
              STORAGE_BACKEND: 's3',
              S3_KEY_PREFIX: compose.S3_KEY_PREFIX,
              DATABASE_URL: compose.DATABASE_URL,
            }
          : {
              BROKER_URL: '',
              STORAGE_BACKEND: 'local',
              DATABASE_URL: `sqlite:///${posixDataDir}/e2e.sqlite3`,
            }),
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

    if (compose) {
      // 派发器与 Worker 是独立进程：它们必须看到与 API 同一套队列/存储配置，否则任务停在 queued。
      const queueEnv: Record<string, string> = {
        APP_ENV: 'development',
        EXTRACTOR_MODE: 'mock',
        BROKER_URL: compose.BROKER_URL,
        QUEUE_NAME: compose.QUEUE_NAME,
        DATABASE_URL: compose.DATABASE_URL,
        STORAGE_BACKEND: 's3',
        S3_KEY_PREFIX: compose.S3_KEY_PREFIX,
        DATA_DIR: posixDataDir,
        UPLOAD_DIR: `${posixDataDir}/uploads`,
        OUTPUT_DIR: `${posixDataDir}/outputs`,
      }
      const dispatcherPid = await startServer(
        [VENV_PY, '-m', 'app.dispatcher'],
        repoRoot,
        'dispatcher',
        'dispatcher.log',
        queueEnv,
      )
      started.push({ pid: dispatcherPid, role: 'dispatcher', log: path.join(tmpDir, 'dispatcher.log') })
      const workerPid = await startServer(
        [VENV_PY, '-m', 'app.worker'],
        repoRoot,
        'worker',
        'worker.log',
        queueEnv,
      )
      started.push({ pid: workerPid, role: 'worker', log: path.join(tmpDir, 'worker.log') })
      process.stdout.write(`[setup] dispatcher pid=${dispatcherPid} worker pid=${workerPid}\n`)
      await waitQueueReady(90_000)
    }
  }

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

  writeFileSync(
    SERVERS_FILE,
    JSON.stringify({ started, compose: COMPOSE_MODE, appContainers: APP_CONTAINER_MODE, run: RUN_ID }),
  )

  // 首个页面加载会触发 Vite 预构建，先预热一次，避免第一条用例把编译时间算进断言超时。
  const warm = await fetch(`${FRONTEND_ORIGIN}/`)
  if (!(await warm.text()).includes('DocAI')) {
    throw new Error('frontend served unexpected index.html')
  }
  process.stdout.write('[setup] warm request ok\n')
}
