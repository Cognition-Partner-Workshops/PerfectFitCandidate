/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_PFC_API?: string
  readonly VITE_PFC_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
