import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { api, ApiError } from '../../api'
import { MockApiClient } from '../../api/mock/mockClient'
import { useAuth } from '../../auth/context'
import { checkPassword } from '../../lib/password'
import { Banner } from '../../components/ui'
import { PasswordChecklist } from './PasswordChecklist'

/** First-run admin setup — no default password ships (REQ-AUTH-03, GUI_OCC_DESIGN.md §7.2, TDD.md §9.1). */
export function SetupPage() {
  const { adminExists, refresh } = useAuth()
  const navigate = useNavigate()
  const [step, setStep] = useState(1)
  const [setupCode, setSetupCode] = useState('')
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [dataFolder, setDataFolder] = useState('data\\')
  const [resumeFolder, setResumeFolder] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (adminExists && step === 1) return <Navigate to="/login" replace />
  const policy = checkPassword(password, 'Admin', { email, name: fullName })
  const canCreate = setupCode.trim() && fullName.trim() && /.+@.+\..+/.test(email) && policy.ok && password === confirm
  const mockCode = api instanceof MockApiClient ? api.setupCode : null

  const create = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.setupAdmin({ setupCode: setupCode.trim(), fullName: fullName.trim(), email: email.trim(), password })
      setPassword('')
      setConfirm('')
      setStep(2)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Setup failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-card" style={{ width: 'min(560px, 100%)' }}>
        <div className="card stack">
          <div className="row">
            <h1>Welcome to PerfectFitCandidate — first-run setup</h1>
            <span className="spacer" />
            <span className="muted small">Step {step}/3</span>
          </div>
          {step === 1 && (
            <form className="stack" onSubmit={create} aria-label="Create the OCC administrator account">
              <h2>Create the OCC administrator account</h2>
              {error && <Banner tone="danger">{error}</Banner>}
              <label className="field">
                Setup code
                <input value={setupCode} onChange={(e) => setSetupCode(e.target.value)} autoComplete="off" />
                <span className="small muted">Shown in the PFC console window and in data\run\setup-code.txt.</span>
                {mockCode && <span className="small muted">Mock mode setup code: <span className="mono">{mockCode}</span></span>}
              </label>
              <label className="field">
                Full name
                <input value={fullName} onChange={(e) => setFullName(e.target.value)} />
              </label>
              <label className="field">
                Corporate email
                <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
              </label>
              <label className="field">
                Password
                <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
              </label>
              <label className="field">
                Confirm password
                <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              </label>
              {confirm && password !== confirm && <span className="error-text">Passwords do not match</span>}
              <PasswordChecklist checks={policy.checks} />
              <div className="modal-actions">
                <button className="btn btn-primary" type="submit" disabled={!canCreate || busy}>
                  Create admin
                </button>
              </div>
            </form>
          )}
          {step === 2 && (
            <div className="stack">
              <h2>Confirm folders (optional)</h2>
              <label className="field">
                Data folder
                <input value={dataFolder} onChange={(e) => setDataFolder(e.target.value)} />
              </label>
              <label className="field">
                Resume folder path
                <input value={resumeFolder} onChange={(e) => setResumeFolder(e.target.value)} placeholder="Can be set later in Settings" />
              </label>
              <div className="modal-actions">
                <button className="btn btn-primary" onClick={() => setStep(3)}>
                  Next
                </button>
              </div>
            </div>
          )}
          {step === 3 && (
            <div className="stack">
              <h2>Summary</h2>
              <p>The administrator account for {fullName} has been created. This screen will not appear again unless the data folder is reset.</p>
              <div className="modal-actions">
                <button
                  className="btn btn-primary"
                  onClick={() => {
                    void refresh().then(() => navigate('/login', { replace: true }))
                  }}
                >
                  Go to sign in
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
