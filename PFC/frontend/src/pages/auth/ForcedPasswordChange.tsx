import { Navigate, useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth/context'
import { Banner } from '../../components/ui'
import { ChangePasswordForm } from './ChangePasswordForm'

/** Forced change after an Admin reset or a one-time password (GUI_OCC_DESIGN.md §7.3, REQ-OCC-02). */
export function ForcedPasswordChange() {
  const { user, setUser } = useAuth()
  const navigate = useNavigate()
  if (!user) return <Navigate to="/login" replace />
  if (!user.mustChangePassword) return <Navigate to="/dashboard" replace />
  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="card stack">
          <h1>Set a new password</h1>
          <Banner tone="warning">Your password was reset. Set a new password to continue.</Banner>
          <ChangePasswordForm
            user={user}
            onDone={(u) => {
              setUser(u)
              navigate('/dashboard', { replace: true })
            }}
          />
        </div>
      </div>
    </div>
  )
}
