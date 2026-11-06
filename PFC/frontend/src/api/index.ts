import { apiBaseUrl, apiMode } from '../config'
import type { ApiClient } from './client'
import { HttpApiClient } from './http'
import { MockApiClient } from './mock/mockClient'

export { ApiError } from './client'
export type { ApiClient } from './client'

/** Selected by `VITE_PFC_API` (`mock` default, `http` for the real `/api/v1` backend). */
export const api: ApiClient = apiMode === 'http' ? new HttpApiClient(apiBaseUrl) : new MockApiClient()
