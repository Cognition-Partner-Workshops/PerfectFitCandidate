/**
 * Build-time configuration.
 * The SPA talks to the real `/api/v1` backend by default. The typed in-browser mocks are a dev-only
 * opt-in: `VITE_PFC_API=mock` (any build, used by vitest) or `?mock=1` on a `vite dev` URL.
 */
export type ApiMode = 'mock' | 'http'

export function resolveApiMode(env: { VITE_PFC_API?: string; DEV?: boolean }, search: string): ApiMode {
  if (env.VITE_PFC_API === 'mock') return 'mock'
  if (env.VITE_PFC_API === 'http') return 'http'
  return env.DEV && new URLSearchParams(search).get('mock') === '1' ? 'mock' : 'http'
}

export const apiMode: ApiMode = resolveApiMode(
  import.meta.env,
  typeof window === 'undefined' ? '' : window.location.search,
)
export const apiBaseUrl: string = import.meta.env.VITE_PFC_API_BASE ?? '/api/v1'
export const appVersion = '1.0.0'

/** Phase-gated features (GUI_OCC_DESIGN.md §6.2, §18). Phase-2/3 tabs are hidden, not disabled. */
export const featureFlags = {
  chat: false,
  pdfReport: false,
  smtp: false,
  externalLlm: false,
  autoSync: false,
} as const
