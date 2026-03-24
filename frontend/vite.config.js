import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // traffic‑oracle endpoints
      '/api/v1': {
        target: 'http://localhost:8081',
        changeOrigin: true,
      },
      // convoy‑brain REST — strip /api/agent prefix so /api/agent/health → /health
      '/api/agent': {
        target: 'http://localhost:8082',
        changeOrigin: true,
        timeout: 300000,
        rewrite: (path) => path.replace(/^\/api\/agent/, ''),
      },
      // convoy‑brain chat streaming
      '/chat': {
        target: 'http://localhost:8082',
        changeOrigin: true,
      },
      // convoy‑brain health
      '/health': {
        target: 'http://localhost:8082',
        changeOrigin: true,
      },
      // WebSocket convoy tracking
      '/ws': {
        target: 'ws://localhost:8082',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
