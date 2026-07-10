import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// AI-Test-Gen 前端 Vite 配置
//
// 设计依据：
// - docs/00-architecture-overview.md §2（访问层）
// - docs/05-technical-selection.md §6（FastAPI 后端）
//
// 代理策略：
// Docker 内：API 通过 Nginx 反向代理（/api → backend:8000）
// 本地开发：Vite proxy 直接转发（/api → localhost:8000）

export default defineConfig({
  plugins: [react()],

  server: {
    port: 5173,
    // Docker 内开发模式需要绑定 0.0.0.0
    host: true,
    proxy: {
      '/api': {
        // Docker Compose 网络内通过服务名访问后端
        // 本地开发时需改为 http://localhost:8000
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
        // SSE 支持
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            // 确保 SSE 头部透传
            if (proxyReq.getHeader('accept')?.includes('text/event-stream')) {
              proxyReq.setHeader('Connection', 'keep-alive')
            }
          })
        },
      },
    },
  },

  build: {
    outDir: 'dist',
    // 生产构建优化
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', 'react-router-dom'],
          ui: ['lucide-react', 'recharts'],
        },
      },
    },
  },
})
