import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/** FastAPI port (`PFC_PORT`, default 8765). The backend rejects foreign Host/Origin headers, so both are rewritten. */
const backend = `http://127.0.0.1:${process.env.PFC_PORT ?? '8765'}`

export default defineConfig({
  plugins: [react()],
  // ECharts is isolated in the lazily loaded Dashboard chunk; served from 127.0.0.1 only.
  build: { chunkSizeWarningLimit: 650 },
  server: {
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (req) => {
            if (req.getHeader('origin')) req.setHeader('origin', backend)
          })
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    env: { VITE_PFC_API: 'mock' },
  },
})
