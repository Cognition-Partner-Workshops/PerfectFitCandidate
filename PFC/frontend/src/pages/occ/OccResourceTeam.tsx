import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { ResourceTeamMember } from '../../api/types'
import { EmptyState, ErrorState, Spinner } from '../../components/ui'
import { EMAIL_SYNTAX } from '../../lib/occ'

/** Resource-team CC list (REQ-EML-05, GUI_OCC_DESIGN.md §16.3). Admin-entered addresses only. */
export function OccResourceTeam() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['occ', 'resource-team'], queryFn: () => api.listResourceTeam() })
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const save = useMutation({ mutationFn: (m: ResourceTeamMember[]) => api.saveResourceTeam(m), onSuccess: (m) => qc.setQueryData(['occ', 'resource-team'], m),
    onError: () => void qc.invalidateQueries({ queryKey: ['occ', 'resource-team'] }),
  })
  if (list.isLoading) return <Spinner />
  if (list.isError) return <ErrorState error={list.error} />
  const members = list.data ?? []
  const normalised = email.trim().toLowerCase()
  const duplicate = members.some((m) => m.email === normalised)
  return (
    <section className="card stack" aria-label="Resource-team list">
      <h2>Resource-team email list</h2>
      {save.isError && <ErrorState error={save.error} />}
      {members.length === 0 ? (
        <EmptyState message="No resource-team recipients — candidate emails will be sent without CC." />
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>Email</th>
              <th>Display name</th>
              <th>Active</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.email}>
                <td className="mono">{m.email}</td>
                <td>{m.displayName}</td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Active ${m.email}`}
                    checked={m.active}
                    onChange={() => save.mutate(members.map((x) => (x.email === m.email ? { ...x, active: !x.active } : x)))}
                  />
                </td>
                <td>
                  <button className="btn btn-sm" onClick={() => save.mutate(members.filter((x) => x.email !== m.email))}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <label className="field">
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label className="field">
          Display name
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <button
          className="btn btn-primary"
          disabled={!EMAIL_SYNTAX.test(normalised) || duplicate}
          onClick={() => {
            save.mutate([...members, { email: normalised, displayName: name.trim(), active: true, notes: '' }])
            setEmail('')
            setName('')
          }}
        >
          Add
        </button>
      </div>
      {email && !EMAIL_SYNTAX.test(normalised) && <span className="error-text">Enter a valid email address</span>}
      {duplicate && <span className="error-text">Already in the list</span>}
      <span className="small muted">Changes apply to new drafts only.</span>
    </section>
  )
}
