import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 全局 teardown：只清理 globalSetup 自己启动的进程（taskkill /T 含子进程树），
 * 复用的服务不动。任务要求的「服务进程必须清理不残留」由此处保证。
 */

const testsDir = path.dirname(fileURLToPath(import.meta.url))
const SERVERS_FILE = path.join(testsDir, '.tmp', 'servers.json')

export default function globalTeardown(): void {
  if (!existsSync(SERVERS_FILE)) return
  const data = JSON.parse(readFileSync(SERVERS_FILE, 'utf8')) as {
    started: { pid: number; role: string }[]
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
}