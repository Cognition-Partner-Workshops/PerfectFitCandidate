import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  // ECharts is isolated in the lazily loaded Dashboard chunk; served from 127.0.0.1 only.
  build: { chunkSizeWarningLimit: 650 },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
