import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { replayPlugin } from './server/replay.ts'

export default defineConfig({
  plugins: [react(), replayPlugin()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5196',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (request) => request.setHeader('Origin', 'http://127.0.0.1:5196'))
        },
      },
    },
  },
})
