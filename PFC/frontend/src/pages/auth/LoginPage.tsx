import { useEffect, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { Eye, EyeOff } from 'lucide-react'
import { ApiError } from '../../api'
import type { Role } from '../../api/types'
import { useAuth, LAST_ROLE_KEY, LAST_ROUTE_KEY } from '../../auth/context'
import { Banner } from '../../components/ui'
import { appVersion } from '../../config'

function mmss(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** Login with role dropdown (REQ-AUTH-01..04, GUI_OCC_DESIGN.md §7.1, §7.3). */
export function LoginPage() {
  const { login, user, adminExists, unreachable, refresh, signedOutReason } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [role, setRole] = useState<Role>(() => (localStorage.getItem(LAST_ROLE_KEY) === 'Admin' ? 'Admin' : 'Analyst'))
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [caps, setCaps] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lockedFor, setLockedFor] = useState(0)

  useEffect(() => {
    if (lockedFor <= 0) return
    const t = window.setTimeout(() => setLockedFor((s) => s - 1), 1000)
    return () => window.clearTimeout(t)
  }, [lockedFor])

  if (adminExists === false) return <Navigate to="/setup" replace />
  if (user) return <Navigate to={user.mustChangePassword ? '/change-password' : '/dashboard'} replace />

  const from = (location.state as { from?: string } | null)?.from
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => setCaps(e.getModifierState?.('CapsLock') ?? false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const u = await login({ role, email: email.trim(), password })
      const last = from ?? localStorage.getItem(LAST_ROUTE_KEY)
      navigate(u.mustChangePassword ? '/change-password' : last && last !== '/login' ? last : '/dashboard', { replace: true })
    } catch (err) {
      setPassword('')
      if (err instanceof ApiError && err.code === 'ACCOUNT_LOCKED') setLockedFor(err.problem.retryAfterSeconds ?? 900)
      else if (err instanceof ApiError && err.code === 'ACCOUNT_DISABLED') setError('This account is disabled. Contact your PFC administrator.')
      else if (err instanceof ApiError && err.status === 0) setError("Can't reach the PFC service. Start PFC from the launcher.")
      else setError('Email, password or role is incorrect.')
      document.getElementById('login-password')?.focus()
    } finally {
      setBusy(false)
    }
  }

  const locked = lockedFor > 0
  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="logo">PFC</div>
          <h1>PerfectFitCandidate</h1>
          <p className="muted">Bench-to-demand matching for human review</p>
        </div>
        <form className="card stack" onSubmit={submit} aria-label="Sign in">
          <h2>Sign in</h2>
          {unreachable && (
            <Banner tone="danger">
              Can't reach the PFC service. Start PFC from the launcher.{' '}
              <button type="button" className="btn btn-sm" onClick={() => void refresh()}>
                Retry
              </button>
            </Banner>
          )}
          {signedOutReason === 'timeout' && <Banner tone="info">You were signed out after inactivity.</Banner>}
          {signedOutReason === 'expired' && <Banner tone="info">Your session ended. Sign in again to continue.</Banner>}
          {error && <Banner tone="danger">{error}</Banner>}
          {locked && <Banner tone="warning">Too many attempts. Try again in {mmss(lockedFor)}.</Banner>}
          <label className="field">
            Role
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="Analyst">Analyst</option>
              <option value="Admin">Admin</option>
            </select>
          </label>
          <label className="field">
            Corporate email
            <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label className="field" htmlFor="login-password">
            Password
            <span className="input-group">
              <input
                id="login-password"
                type={showPw ? 'text' : 'password'}
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyUp={onKey}
                onKeyDown={onKey}
              />
              <button type="button" className="icon-btn" aria-label={showPw ? 'Hide password' : 'Show password'} onClick={() => setShowPw((v) => !v)}>
                {showPw ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </span>
          </label>
          {caps && <span className="small" style={{ color: 'var(--color-warning)' }}>⚠ Caps Lock is on</span>}
          <button className="btn btn-primary" type="submit" disabled={busy || locked || !email.trim() || !password}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
          <p className="small muted">Forgot password? Contact your PFC administrator.</p>
        </form>
        <div className="auth-footer">Local instance · 127.0.0.1 · v{appVersion}</div>
      </div>
    </div>
  )
}
