import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// DocAI 演示前端：dev 代理把 /api 转发到本地 FastAPI 后端（只读，后端零改动）
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
})