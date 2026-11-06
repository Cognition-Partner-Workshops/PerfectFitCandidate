/**
 * Build-time configuration.
 * `VITE_PFC_API=http` switches the SPA from the typed in-browser mocks to the real `/api/v1` backend.
 */
export type ApiMode = 'mock' | 'http'

export const apiMode: ApiMode = import.meta.env.VITE_PFC_API === 'http' ? 'http' : 'mock'
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
