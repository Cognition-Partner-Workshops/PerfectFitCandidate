import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../../api'
import type { CurrentUser } from '../../api/types'
import { checkPassword } from '../../lib/password'
import { Banner } from '../../components/ui'
import { PasswordChecklist } from './PasswordChecklist'

/** Change own password (REQ-AUTH-02/03); reused by the forced-change screen and Settings → Account. */
export function ChangePasswordForm({ user, onDone }: { user: CurrentUser; onDone: (u: CurrentUser) => void }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [ok, setOk] = useState(false)
  const policy = checkPassword(next, user.role, { email: user.email, name: user.fullName })
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    try {
      const u = await api.changePassword({ currentPassword: current, newPassword: next })
      setCurrent('')
      setNext('')
      setConfirm('')
      setOk(true)
      onDone(u)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Password change failed')
    }
  }
  return (
    <form className="stack" onSubmit={submit} aria-label="Change password">
      {error && <Banner tone="danger">{error}</Banner>}
      {ok && <Banner tone="success">Password changed.</Banner>}
      <label className="field">
        Current password
        <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
      </label>
      <label className="field">
        New password
        <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
      </label>
      <label className="field">
        Confirm new password
        <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      </label>
      {confirm && next !== confirm && <span className="error-text">Passwords do not match</span>}
      <PasswordChecklist checks={policy.checks} />
      <div className="modal-actions">
        <button className="btn btn-primary" type="submit" disabled={!current || !policy.ok || next !== confirm}>
          Change password
        </button>
      </div>
    </form>
  )
}
