import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy } from 'lucide-react'
import { api } from '../../api'
import type { NewUserRequest, OneTimePassword, Persona, Role } from '../../api/types'
import { Banner, Chip, ErrorState, MaskedValue, Modal, Spinner } from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { maskEmail } from '../../lib/mask'

function OtpDialog({ otp, onClose }: { otp: OneTimePassword; onClose: () => void }) {
  return (
    <Modal title="One-time password" onClose={onClose} actions={<button className="btn btn-primary" onClick={onClose}>Done</button>}>
      <Banner tone="warning">Shown once. Share it securely; the user must change it at first sign-in.</Banner>
      <div className="row">
        <span className="mono" data-testid="otp" style={{ fontSize: 18 }}>
          {otp.oneTimePassword}
        </span>
        <button className="btn btn-sm" onClick={() => void navigator.clipboard?.writeText(otp.oneTimePassword)}>
          <Copy size={14} /> Copy
        </button>
      </div>
    </Modal>
  )
}

/** Users & passwords (GUI_OCC_DESIGN.md §16.2, REQ-AUTH-02/03). */
export function OccUsers() {
  const qc = useQueryClient()
  const users = useQuery({ queryKey: ['occ', 'users'], queryFn: () => api.listUsers() })
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState<NewUserRequest>({ fullName: '', email: '', role: 'Analyst', persona: 'A' })
  const [otp, setOtp] = useState<OneTimePassword | null>(null)
  const refresh = () => void qc.invalidateQueries({ queryKey: ['occ', 'users'] })
  const create = useMutation({
    mutationFn: () => api.createUser(form),
    onSuccess: (o) => {
      setAdding(false)
      setOtp(o)
      setForm({ fullName: '', email: '', role: 'Analyst', persona: 'A' })
      refresh()
    },
  })
  const reset = useMutation({ mutationFn: (id: number) => api.resetPassword(id), onSuccess: (o) => { setOtp(o); refresh() } })
  const update = useMutation({ mutationFn: (v: { id: number; patch: Parameters<typeof api.updateUser>[1] }) => api.updateUser(v.id, v.patch), onSuccess: refresh })
  const unlock = useMutation({ mutationFn: (id: number) => api.unlockUser(id), onSuccess: refresh })
  if (users.isLoading) return <Spinner />
  if (users.isError) return <ErrorState error={users.error} />
  const err = update.error ?? reset.error ?? unlock.error
  return (
    <section className="card stack" aria-label="Users and passwords">
      <div className="row">
        <h2>Users &amp; passwords</h2>
        <span className="spacer" />
        <button className="btn btn-primary" onClick={() => setAdding(true)}>
          + Add user
        </button>
      </div>
      {err && <ErrorState error={err} />}
      <table className="data">
        <thead>
          <tr>
            <th>Name</th>
            <th>Email</th>
            <th>Role</th>
            <th>Persona</th>
            <th>Status</th>
            <th>Last sign-in</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {(users.data ?? []).map((u) => (
            <tr key={u.userId}>
              <td>{u.fullName}</td>
              <td>
                <MaskedValue masked={maskEmail(u.email)} entity="occ_user" id={u.userId} />
              </td>
              <td>
                <select
                  aria-label={`Role for ${u.fullName}`}
                  value={u.role}
                  onChange={(e) => {
                    if (window.confirm(`Change role of ${u.fullName} to ${e.target.value}?`)) update.mutate({ id: u.userId, patch: { role: e.target.value as Role } })
                  }}
                >
                  <option>Analyst</option>
                  <option>Admin</option>
                </select>
              </td>
              <td>{u.persona ?? '—'}</td>
              <td>
                <Chip tone={u.status === 'Active' ? 'success' : u.status === 'Locked' ? 'warning' : 'neutral'}>{u.status}</Chip>
              </td>
              <td>{u.lastSignIn ? formatDateTime(u.lastSignIn) : '—'}</td>
              <td className="row" style={{ flexWrap: 'nowrap' }}>
                {u.status === 'Locked' && (
                  <button className="btn btn-sm" onClick={() => unlock.mutate(u.userId)}>
                    Unlock
                  </button>
                )}
                <button className="btn btn-sm" onClick={() => reset.mutate(u.userId)}>
                  Reset password
                </button>
                <button className="btn btn-sm" onClick={() => update.mutate({ id: u.userId, patch: { status: u.status === 'Disabled' ? 'Active' : 'Disabled' } })}>
                  {u.status === 'Disabled' ? 'Enable' : 'Disable'}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {adding && (
        <Modal
          title="Add user"
          onClose={() => setAdding(false)}
          actions={
            <>
              <button className="btn" onClick={() => setAdding(false)}>
                Cancel
              </button>
              <button className="btn btn-primary" disabled={!form.fullName.trim() || !/.+@.+\..+/.test(form.email) || create.isPending} onClick={() => create.mutate()}>
                Create and generate one-time password
              </button>
            </>
          }
        >
          <label className="field">
            Name
            <input value={form.fullName} onChange={(e) => setForm({ ...form, fullName: e.target.value })} />
          </label>
          <label className="field">
            Corporate email
            <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          </label>
          <label className="field">
            Role
            <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              <option>Analyst</option>
              <option>Admin</option>
            </select>
          </label>
          <label className="field">
            Persona (informational)
            <select value={form.persona} onChange={(e) => setForm({ ...form, persona: e.target.value as Persona })}>
              <option value="A">A — matching and candidate email</option>
              <option value="B">B — demand analytics</option>
              <option value="C">C — bench analytics</option>
            </select>
          </label>
          {create.isError && <ErrorState error={create.error} />}
        </Modal>
      )}
      {otp && <OtpDialog otp={otp} onClose={() => setOtp(null)} />}
    </section>
  )
}
