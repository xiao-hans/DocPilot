import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// DocAI 演示前端：dev 代理把 /api 转发到本地 FastAPI 后端。
// 端到端用例用独立端口起一次性后端，代理目标随之可配，避免测试写进开发数据。
// 用 loadEnv 而不是 process.env：本仓库不装 @types/node，直接引用会让 `npm run build` 的类型检查失败。
export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: loadEnv(mode, '.', 'DOCPILOT_').DOCPILOT_API_ORIGIN || 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
}))
