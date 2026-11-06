import { apiBaseUrl, apiMode } from '../config'
import type { ApiClient } from './client'
import { HttpApiClient } from './http'
export { setUnauthorizedHandler } from './http'
import { MockApiClient } from './mock/mockClient'

export { ApiError } from './client'
export type { ApiClient } from './client'

/** Real `/api/v1` backend by default; the mock only when opted in (see config.ts / README). */
export const api: ApiClient = apiMode === 'http' ? new HttpApiClient(apiBaseUrl) : new MockApiClient()
