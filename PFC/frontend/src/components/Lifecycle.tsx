import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { Entity, LifecycleDetail } from '../api/maintenance'
import { Banner, ErrorState, Modal, Spinner } from './ui'
import { formatDate, formatDateTime } from '../lib/format'
import { countLabel, reasonLabel } from '../lib/lifecycle'

export function LifecycleMenu({ entity, id }: { entity: Entity; id: number }) {
  const [open, setOpen] = useState(false)
  return <>
    <button className="icon-btn" aria-label="Lifecycle" title="Lifecycle" aria-haspopup="dialog" onClick={() => setOpen(true)}>⋯</button>
    {open && <Modal title="Lifecycle actions" onClose={() => setOpen(false)}><LifecycleControls entity={entity} id={id} /></Modal>}
  </>
}

export function LifecycleHistory({ detail }: { detail: LifecycleDetail }) {
  return <section className="card stack" aria-label="Lifecycle history">
    <h3>Lifecycle history</h3>
    {detail.lifecycle_events.length === 0 ? <p className="muted">No lifecycle events recorded.</p> :
      <ol className="stack">{detail.lifecycle_events.map((event) => <li key={event.event_id}>
        <strong>{reasonLabel(event.to_state)}</strong> · {formatDateTime(event.created_at)}
        <div className="small muted">{event.from_state ? `${reasonLabel(event.from_state)} → ` : ''}{reasonLabel(event.to_state)} · {reasonLabel(event.source)}{event.outcome ? ` · ${reasonLabel(event.outcome)}` : ''}{event.reason_code ? ` · ${reasonLabel(event.reason_code)}` : ''}</div>
        {event.comment && <p>{event.comment}</p>}
      </li>)}</ol>}
  </section>
}

export function LifecycleDialog({ entity, id, detail, reopen, onClose }: {
  entity: Entity; id: number; detail: LifecycleDetail; reopen: boolean; onClose: () => void
}) {
  const qc = useQueryClient()
  const reasons = useQuery({ queryKey: ['lifecycle-reasons'], queryFn: () => api.maintenance.reasons() })
  const [outcome, setOutcome] = useState(reopen ? 'reopen' : entity === 'demand' ? 'filled' : 'off_bench')
  const [reason, setReason] = useState('')
  const [comment, setComment] = useState('')
  const save = useMutation({
    mutationFn: () => api.maintenance.change(entity, id, reopen ? 'reopen' : 'end', {
      ...(!reopen ? { outcome } : {}), reason_code: reason, comment: comment.trim() || null,
      expected_updated_at: detail.updated_at,
      expected_event_id: Math.max(0, ...detail.lifecycle_events.map((event) => event.event_id)),
    }),
    onSuccess: () => { void qc.invalidateQueries(); onClose() },
  })
  const outcomes = reopen ? ['reopen'] : entity === 'demand' ? ['filled', 'cancelled', 'closed'] : ['off_bench', 'released']
  return <Modal title={reopen ? 'Reopen record' : 'End of cycle…'} onClose={onClose} actions={<>
    <button className="btn" onClick={onClose}>Cancel</button>
    <button className="btn btn-primary" disabled={save.isPending || !reason || (reason === 'other' && !comment.trim()) || reasons.isError} onClick={() => save.mutate()}>{reopen ? 'Reopen' : 'End cycle'}</button>
  </>}>
    <p>{reopen ? 'Return this record to the active list.' : 'Remove this record from active lists and block its pending emails. It can be reopened before archival.'}</p>
    {reasons.isLoading && <Spinner />}{reasons.isError && <ErrorState error={reasons.error} />}
    <label className="field">Outcome<select value={outcome} disabled={reopen} onChange={(e) => { setOutcome(e.target.value); setReason('') }}>
      {outcomes.map((value) => <option key={value} value={value}>{reasonLabel(value)}</option>)}
    </select></label>
    <label className="field">Reason (required)<select value={reason} onChange={(e) => setReason(e.target.value)}>
      <option value="">Choose a reason…</option>{(reasons.data?.[outcome] ?? []).map((value) => <option key={value} value={value}>{reasonLabel(value)}</option>)}
    </select></label>
    <label className="field">Comment{reason === 'other' ? ' (required)' : ' (optional)'}<textarea maxLength={500} required={reason === 'other'} value={comment} onChange={(e) => setComment(e.target.value)} /></label>
    <span className="small muted">{comment.length}/500 · Contact details are redacted before storing.</span>
    {save.isError && <ErrorState error={save.error} />}
  </Modal>
}

export function LifecycleControls({ entity, id, showHistory = false, defer = false }: { entity: Entity; id: number; showHistory?: boolean; defer?: boolean }) {
  const [action, setAction] = useState<'end' | 'reopen' | null>(null)
  const [history, setHistory] = useState(showHistory)
  const detail = useQuery({ queryKey: ['lifecycle', entity, id], queryFn: () => api.maintenance.detail(entity, id), enabled: !defer || action !== null || history })
  if (detail.isLoading) return <Spinner label="Loading lifecycle…" />
  if (detail.isError) return <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
  if (!detail.data) return <div className="stack">
    <button className="btn btn-sm" onClick={() => setAction('end')}>End of cycle…</button>
    <button className="btn btn-sm" onClick={() => setHistory(true)}>Lifecycle history</button>
  </div>
  const data = detail.data
  const archived = data.lifecycle === 'archived'
  const archivedAt = [...data.lifecycle_events].reverse().find((event) => event.to_state === 'archived')?.created_at
  const manuallyEnded = data.end_source === 'manual' && ['closed', 'off_bench'].includes(data.lifecycle)
  return <div className="stack">
    {archived && <Banner tone="info">Archived on {formatDate(archivedAt ?? null)} · Read-only</Banner>}
    <div className="row">
      {!archived && (manuallyEnded ? <button className="btn btn-sm" onClick={() => setAction('reopen')}>Reopen</button> :
        <button className="btn btn-sm" onClick={() => setAction('end')}>End of cycle…</button>)}
      <button className="btn btn-sm" aria-expanded={history} onClick={() => setHistory(!history)}>Lifecycle history</button>
    </div>
    {history && <LifecycleHistory detail={data} />}
    {action && <LifecycleDialog entity={entity} id={id} detail={data} reopen={action === 'reopen'} onClose={() => setAction(null)} />}
  </div>
}

export function EndedRecords({ entity }: { entity: Entity }) {
  const [open, setOpen] = useState(false)
  const [page, setPage] = useState(1)
  const ended = useQuery({ queryKey: ['ended', entity, page], queryFn: () => api.maintenance.ended(entity, page), enabled: open })
  return <section className="stack">
    <button className="btn" aria-expanded={open} onClick={() => setOpen(!open)}>Ended {entity === 'demand' ? 'demands' : 'resources'}</button>
    {open && <div className="card stack"><p>Ended records are not in the active list. Manually ended records can be reopened before archival.</p>
      {ended.isLoading && <Spinner />}{ended.isError && <ErrorState error={ended.error} />}
      {ended.data?.items.length === 0 && <p>No ended records.</p>}
      {ended.data?.items.map((row) => <div className="row" key={row.demand_id ?? row.resource_id}><strong>{row.code_requisition ?? row.short_name}</strong><LifecycleControls entity={entity} id={(row.demand_id ?? row.resource_id)!} /></div>)}
      {ended.data && <div className="row"><span>{countLabel(ended.data.total, 'ended record')} · Page {page}</span><button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous ended records</button><button className="btn" disabled={page * 25 >= ended.data.total} onClick={() => setPage(page + 1)}>Next ended records</button></div>}
    </div>}
  </section>
}
