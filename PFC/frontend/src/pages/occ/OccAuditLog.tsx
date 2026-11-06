import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api'
import type { AuditQuery } from '../../api/types'
import { EmptyState, ErrorState, Spinner } from '../../components/ui'
import { auditToCsv } from '../../lib/csv'
import { downloadText } from '../../lib/download'
import { formatDateTime } from '../../lib/format'

/** Append-only audit log viewer with filters and CSV export (REQ-AUTH-05, GUI_OCC_DESIGN.md §16.12). */
export function OccAuditLog() {
  const [query, setQuery] = useState<AuditQuery>({})
  const q = useQuery({ queryKey: ['occ', 'audit', query], queryFn: () => api.auditLog(query) })
  const all = useQuery({ queryKey: ['occ', 'audit', {}], queryFn: () => api.auditLog({}) })
  const [expanded, setExpanded] = useState<number | null>(null)
  const rows = q.data ?? []
  const options = (k: 'action' | 'entity') => [...new Set((all.data ?? []).map((a) => a[k]))].sort()
  const setField = (k: keyof AuditQuery, v: string) => setQuery((cur) => ({ ...cur, [k]: v || undefined }))
  return (
    <section className="card stack" aria-label="Audit log">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h2>Audit log</h2>
        <button className="btn" disabled={!rows.length} onClick={() => downloadText(auditToCsv(rows), `PFC-audit-log-${new Date().toISOString().slice(0, 10)}.csv`)}>
          Export CSV ({rows.length})
        </button>
      </div>
      <p className="meta">Append-only. Logins, unmasking, OCC changes, email approvals and sends, settings changes.</p>
      <div className="row" style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <label className="field">
          From
          <input type="date" value={query.from ?? ''} onChange={(e) => setField('from', e.target.value)} />
        </label>
        <label className="field">
          To
          <input type="date" value={query.to ?? ''} onChange={(e) => setField('to', e.target.value)} />
        </label>
        <label className="field">
          User
          <input value={query.user ?? ''} onChange={(e) => setField('user', e.target.value)} placeholder="Name" />
        </label>
        <label className="field">
          Action
          <select value={query.action ?? ''} onChange={(e) => setField('action', e.target.value)}>
            <option value="">All</option>
            {options('action').map((a) => (
              <option key={a}>{a}</option>
            ))}
          </select>
        </label>
        <label className="field">
          Entity
          <select value={query.entity ?? ''} onChange={(e) => setField('entity', e.target.value)}>
            <option value="">All</option>
            {options('entity').map((a) => (
              <option key={a}>{a}</option>
            ))}
          </select>
        </label>
        <button className="btn" onClick={() => setQuery({})}>
          Clear
        </button>
      </div>
      {q.isLoading ? (
        <Spinner />
      ) : q.isError ? (
        <ErrorState error={q.error} />
      ) : rows.length === 0 ? (
        <EmptyState message="No audit events match these filters." />
      ) : (
        <div className="table-wrap" style={{ maxHeight: 520 }}>
          <table className="data" aria-label="Audit events">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>User</th>
                <th>Role</th>
                <th>Action</th>
                <th>Entity</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.auditId} onClick={() => a.detailsJson && setExpanded(expanded === a.auditId ? null : a.auditId)} style={a.detailsJson ? { cursor: 'pointer' } : undefined}>
                  <td className="mono">{formatDateTime(a.at)}</td>
                  <td>{a.user}</td>
                  <td>{a.role}</td>
                  <td>{a.action}</td>
                  <td className="mono">{a.entity}</td>
                  <td>
                    {a.summary}
                    {expanded === a.auditId && <pre className="mono meta" style={{ whiteSpace: 'pre-wrap' }}>{a.detailsJson}</pre>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
