import { createContext, useContext } from 'react'
import type { CurrentUser, LoginRequest } from '../api/types'

export interface AuthState {
  user: CurrentUser | null
  loading: boolean
  /** null = unknown (still loading); false = first-run setup required (REQ-AUTH-03). */
  adminExists: boolean | null
  /** Backend not reachable (GUI_OCC_DESIGN.md §7.3). */
  unreachable: boolean
  login: (req: LoginRequest) => Promise<CurrentUser>
  logout: (reason?: 'timeout') => Promise<void>
  refresh: () => Promise<void>
  setUser: (user: CurrentUser) => void
  signedOutReason: 'timeout' | 'expired' | null
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}

export const LAST_ROLE_KEY = 'pfc.lastRole'
export const LAST_ROUTE_KEY = 'pfc.lastRoute'
