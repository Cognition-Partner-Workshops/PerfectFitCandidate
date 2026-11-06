import type { ReactNode } from 'react'
import { Link, Navigate, useLocation } from 'react-router-dom'
import type { Role } from '../api/types'
import { useAuth } from './context'
import { Spinner } from '../components/ui'

/**
 * Login and role routing (GUI_OCC_DESIGN.md §7.4): no admin → first-run setup; no session →
 * login (remembering the route); password reset pending → forced change.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading, adminExists } = useAuth()
  const location = useLocation()
  if (loading) return <Spinner />
  if (adminExists === false) return <Navigate to="/setup" replace />
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  if (user.mustChangePassword && location.pathname !== '/change-password') return <Navigate to="/change-password" replace />
  return <>{children}</>
}

/** Role guard: Analysts opening `#/occ/*` see the no-access page (GUI_OCC_DESIGN.md §4.3). Server still enforces. */
export function RequireRole({ role, children }: { role: Role; children: ReactNode }) {
  const { user } = useAuth()
  if (user?.role !== role) return <NoAccess />
  return <>{children}</>
}

export function NoAccess() {
  return (
    <div className="page">
      <div className="card empty" role="alert">
        <h1>You don't have access to this area</h1>
        <p className="muted">This section is available to the OCC Admin only.</p>
        <Link className="btn btn-primary" to="/dashboard">
          Back to Dashboard
        </Link>
      </div>
    </div>
  )
}
