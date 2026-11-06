import { defineConfig, devices } from '@playwright/test'

/** Smoke test against the real FastAPI backend serving the built SPA (`npm run build` first). */
const port = Number(process.env.PFC_E2E_PORT ?? 8799)
const python = process.env.PFC_PYTHON ?? 'python'

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: { baseURL: `http://127.0.0.1:${port}`, trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `${python} e2e/serve.py`,
    url: `http://127.0.0.1:${port}/api/v1/setup/status`,
    env: { PFC_PORT: String(port) },
    reuseExistingServer: false,
    timeout: 60_000,
  },
})
