import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { ApprovalDecision, ApprovalEntry } from '../../api/types'
import { Banner, Chip, EmptyState, ErrorState, Modal, Spinner } from '../../components/ui'
import { formatDateTime } from '../../lib/format'

type Pending = { entry: ApprovalEntry; action: 'modify' | 'revert' }

/** DL/CT approvals: approve, modify (reason required) or revert uploaded changes (REQ-ROLE-03, REQ-ING-03, GUI_OCC_DESIGN.md §16.4). */
export function OccApprovals() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['occ', 'approvals'], queryFn: () => api.listApprovals() })
  const [source, setSource] = useState<'DL' | 'CT'>('DL')
  const [showDecided, setShowDecided] = useState(false)
  const [pending, setPending] = useState<Pending | null>(null)
  const [comment, setComment] = useState('')
  const [values, setValues] = useState<Record<string, string>>({})
  const decide = useMutation({
    mutationFn: ({ id, d }: { id: number; d: ApprovalDecision }) => api.decideApproval(id, d),
    onSuccess: () => {
      setPending(null)
      setComment('')
      setValues({})
      void qc.invalidateQueries({ queryKey: ['occ', 'approvals'] })
    },
  })
  if (q.isLoading) return <Spinner />
  if (q.isError) return <ErrorState error={q.error} />
  const all = q.data ?? []
  const rows = all.filter((e) => e.source === source && (showDecided || e.state === 'Pending'))
  const open = (entry: ApprovalEntry, action: Pending['action']) => {
    decide.reset()
    setComment('')
    setValues({})
    setPending({ entry, action })
  }
  return (
    <section className="card stack" aria-label="DL/CT approvals">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h2>DL / CT approvals</h2>
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" checked={showDecided} onChange={(e) => setShowDecided(e.target.checked)} /> Show decided
        </label>
      </div>
      <div className="tabs" role="tablist">
        {(['DL', 'CT'] as const).map((s) => (
          <button key={s} role="tab" aria-selected={source === s} onClick={() => setSource(s)}>
            {s === 'DL' ? 'Demand List' : 'Candidate Tracker'} ({all.filter((e) => e.source === s && e.state === 'Pending').length})
          </button>
        ))}
      </div>
      {decide.isError && !pending && <Banner tone="danger">{(decide.error as Error).message}</Banner>}
      {rows.length === 0 ? (
        <EmptyState message="No pending changes for this source." />
      ) : (
        <table className="data" aria-label={`${source} approvals`}>
          <thead>
            <tr>
              <th>Key</th>
              <th>Change</th>
              <th>Fields (before → after)</th>
              <th>Uploaded</th>
              <th>State</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.entryId}>
                <td className="mono">{e.key}</td>
                <td>
                  <Chip tone={e.change === 'Removed' ? 'danger' : e.change === 'New' ? 'info' : 'neutral'}>{e.change}</Chip>
                  {e.conflict && (
                    <Chip tone="warning" title="An Admin-edited value differs from the upload">
                      ⚠ Conflict
                    </Chip>
                  )}
                </td>
                <td>
                  {e.fields.map((f) => (
                    <div key={f.field}>
                      <strong>{f.field}:</strong> {f.before ?? '—'} → {f.after ?? '—'}
                      {f.adminValue !== undefined && <span className="meta"> (Admin value: {f.adminValue ?? '—'})</span>}
                    </div>
                  ))}
                  {e.comment && <div className="meta">Reason: {e.comment}</div>}
                </td>
                <td>
                  {e.uploadedBy}
                  <div className="meta">{formatDateTime(e.uploadedAt)}</div>
                </td>
                <td>
                  <Chip tone={e.state === 'Pending' ? 'warning' : e.state === 'Reverted' ? 'danger' : 'success'}>{e.state}</Chip>
                </td>
                <td>
                  {e.state === 'Pending' && (
                    <div className="row" style={{ gap: 4 }}>
                      <button className="btn btn-sm btn-primary" aria-label={`Approve ${e.key}`} onClick={() => decide.mutate({ id: e.entryId, d: { action: 'approve' } })}>
                        Approve
                      </button>
                      {e.change !== 'Removed' && (
                        <button className="btn btn-sm" aria-label={`Modify ${e.key}`} onClick={() => open(e, 'modify')}>
                          Modify
                        </button>
                      )}
                      <button className="btn btn-sm" aria-label={`Revert ${e.key}`} onClick={() => open(e, 'revert')}>
                        Revert
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {pending && (
        <Modal
          title={`${pending.action === 'modify' ? 'Modify' : 'Revert'} ${pending.entry.key}`}
          onClose={() => setPending(null)}
          actions={
            <>
              <button className="btn" onClick={() => setPending(null)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                disabled={!comment.trim() || (pending.action === 'modify' && !Object.values(values).some((v) => v.trim()))}
                onClick={() =>
                  decide.mutate({
                    id: pending.entry.entryId,
                    d: { action: pending.action, comment, values: pending.action === 'modify' ? Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim())) : undefined },
                  })
                }
              >
                {pending.action === 'modify' ? 'Save correction' : 'Revert change'}
              </button>
            </>
          }
        >
          <div className="stack">
            {pending.action === 'modify' &&
              pending.entry.fields.map((f) => (
                <label key={f.field} className="field">
                  {f.field}
                  <input placeholder={f.after ?? ''} value={values[f.field] ?? ''} onChange={(ev) => setValues({ ...values, [f.field]: ev.target.value })} />
                </label>
              ))}
            <label className="field">
              Reason (required, audit-logged)
              <textarea rows={3} value={comment} onChange={(ev) => setComment(ev.target.value)} />
            </label>
            {decide.isError && <Banner tone="danger">{(decide.error as Error).message}</Banner>}
          </div>
        </Modal>
      )}
    </section>
  )
}
