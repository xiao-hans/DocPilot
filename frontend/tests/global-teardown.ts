import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 全局 teardown：只清理 globalSetup 自己启动的进程（taskkill /T 含子进程树），
 * 复用的服务不动。任务要求的「服务进程必须清理不残留」由此处保证。
 *
 * Compose 形态还要回收本次的一次性 PostgreSQL 库与 `e2e-<run>/` 对象前缀。这一步必须
 * 在杀进程之后做（连接还挂着时删库会失败），且删除结果由脚本回查确认：确认不了就用
 * 非零退出把"没删干净"报出来，而不是留个残库给下一次运行。
 */

const testsDir = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(testsDir, '..', '..')
const VENV_PY = path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
const COMPOSE_HELPER = path.join(repoRoot, 'scripts', 'e2e_compose_env.py')
const SERVERS_FILE = path.join(testsDir, '.tmp', 'servers.json')

export default function globalTeardown(): void {
  if (!existsSync(SERVERS_FILE)) return
  const data = JSON.parse(readFileSync(SERVERS_FILE, 'utf8')) as {
    started: { pid: number; role: string }[]
    compose?: boolean
    appContainers?: boolean
    run?: string
  }
  for (const s of data.started) {
    const killed = spawnSync('taskkill', ['/pid', String(s.pid), '/T', '/F'], {
      windowsHide: true,
      encoding: 'utf8',
    })
    if (killed.status === 0) {
      process.stdout.write(`[teardown] killed ${s.role} pid=${s.pid}\n`)
    } else {
      try {
        process.kill(s.pid)
        process.stdout.write(`[teardown] fallback-killed ${s.role} pid=${s.pid}\n`)
      } catch {
        process.stdout.write(`[teardown] ${s.role} pid=${s.pid} already gone\n`)
      }
    }
  }

  if (data.compose && data.run) {
    if (data.appContainers) {
      // 容器要先停：删库时 Worker 还连着会把连接一起杀掉，容器侧表现为莫名重启。
      const down = spawnSync(VENV_PY, ['-X', 'utf8', COMPOSE_HELPER, 'app-down'], {
        cwd: repoRoot,
        encoding: 'utf8',
      })
      for (const line of `${down.stdout ?? ''}${down.stderr ?? ''}`.trim().split(/\r?\n/)) {
        process.stdout.write(`[teardown] ${line}\n`)
      }
    }
    const res = spawnSync(
      VENV_PY,
      ['-X', 'utf8', COMPOSE_HELPER, 'teardown', '--run', data.run],
      { cwd: repoRoot, encoding: 'utf8' },
    )
    const out = `${res.stdout ?? ''}${res.stderr ?? ''}`.trim()
    for (const line of out.split(/\r?\n/)) {
      process.stdout.write(`[teardown] ${line}\n`)
    }
    if (res.status !== 0) {
      // 不 throw：teardown 里抛错会把 Playwright 的结论一起盖掉。残留已经由上面几行报明。
      process.stdout.write(`[teardown] Compose 环境回收未完成（退出码 ${res.status}），见上方残留清单\n`)
    }
  }
}
