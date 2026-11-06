import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, ApiError, setUnauthorizedHandler } from '../api'
import type { CurrentUser, LoginRequest } from '../api/types'
import { AuthContext, LAST_ROLE_KEY, type AuthState } from './context'

interface LoadResult {
  adminExists: boolean | null
  user: CurrentUser | null
  unreachable: boolean
}

async function loadSession(): Promise<LoadResult> {
  try {
    const status = await api.getSetupStatus()
    return { adminExists: status.adminExists, user: status.adminExists ? await api.me() : null, unreachable: false }
  } catch (e) {
    if (e instanceof ApiError && e.status === 0) return { adminExists: null, user: null, unreachable: true }
    throw e
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [loading, setLoading] = useState(true)
  const [adminExists, setAdminExists] = useState<boolean | null>(null)
  const [unreachable, setUnreachable] = useState(false)
  const [signedOutReason, setSignedOutReason] = useState<'timeout' | 'expired' | null>(null)

  const apply = useCallback((r: LoadResult) => {
    setUnreachable(r.unreachable)
    setAdminExists(r.adminExists)
    setUser(r.user)
    setLoading(false)
  }, [])

  const refresh = useCallback(async () => apply(await loadSession()), [apply])

  useEffect(() => {
    let alive = true
    void loadSession().then((r) => {
      if (alive) apply(r)
    })
    return () => {
      alive = false
    }
  }, [apply])

  useEffect(() => {
    setUnauthorizedHandler(() => {
      queryClient.clear()
      setUser(null)
      setSignedOutReason('expired')
    })
    return () => setUnauthorizedHandler(null)
  }, [queryClient])

  const login = useCallback(async (req: LoginRequest) => {
    const u = await api.login(req)
    queryClient.clear()
    localStorage.setItem(LAST_ROLE_KEY, req.role)
    setSignedOutReason(null)
    setUser(u)
    return u
  }, [queryClient])

  const logout = useCallback(async (reason?: 'timeout') => {
    await api.logout()
    queryClient.clear()
    setUser(null)
    setSignedOutReason(reason ?? null)
  }, [queryClient])

  const value = useMemo<AuthState>(
    () => ({ user, loading, adminExists, unreachable, login, logout, refresh, setUser, signedOutReason }),
    [user, loading, adminExists, unreachable, login, logout, refresh, signedOutReason],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
