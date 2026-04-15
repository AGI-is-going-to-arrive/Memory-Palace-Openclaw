import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
const apiProxyTarget =
  process.env.MEMORY_PALACE_API_PROXY_TARGET ||
  process.env.NOCTURNE_API_PROXY_TARGET ||
  'http://127.0.0.1:8000'
const sseProxyTarget =
  process.env.MEMORY_PALACE_SSE_PROXY_TARGET ||
  process.env.NOCTURNE_SSE_PROXY_TARGET ||
  apiProxyTarget

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // 避免 Windows 下优先解析 ::1 导致 IPv6 拒绝连接
        target: apiProxyTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, '')
      },
      '/sse/messages': {
        target: sseProxyTarget,
        changeOrigin: true
      },
      '/messages': {
        target: sseProxyTarget,
        changeOrigin: true
      },
      '/sse': {
        target: sseProxyTarget,
        changeOrigin: true
      }
    }
  }
})
