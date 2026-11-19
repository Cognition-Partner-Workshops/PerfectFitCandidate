import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../api'
import type { Collection, Entity } from '../api/maintenance'
import { ErrorState, MaskedValue, Modal, Spinner } from '../components/ui'
import { LifecycleControls } from '../components/Lifecycle'
import { formatDate } from '../lib/format'
import { downloadBlob } from '../lib/download'
import { countLabel } from '../lib/lifecycle'

function ArchivedDetail({ entity, id, onClose }: { entity: Entity; id: number; onClose: () => void }) {
  const detail = useQuery({ queryKey: ['lifecycle', entity, id], queryFn: () => api.maintenance.detail(entity, id) })
  return <Modal title="Archived record — read-only" onClose={onClose}>
    {detail.isLoading && <Spinner />}{detail.isError && <ErrorState error={detail.error} />}
    {detail.data && <div className="stack"><h3>{detail.data.code_requisition ?? detail.data.short_name}</h3><p>{detail.data.demand_request_name}</p>
      <dl>{([
        ['Account', detail.data.account_name], ['Grade', detail.data.code_local_grade ?? detail.data.grade],
        ['Location', detail.data.region ?? detail.data.location], ['BU', detail.data.bu], ['Owner', detail.data.owner],
        ['Bench status', detail.data.bench_status], ['Skills', detail.data.skills_raw],
      ] as const).filter(([, value]) => value).map(([label, value]) => <div className="row" key={label}><dt><strong>{label}</strong></dt><dd>{value}</dd></div>)}</dl>
      {entity === 'resource' && <div>Email: <MaskedValue masked={detail.data.email_address ?? null} entity="resource" id={id} /></div>}
      {detail.data.versions?.map((version) => <details key={version.version_no}><summary>JD version {version.version_no} · {formatDate(version.created_at)}</summary><p style={{ whiteSpace: 'pre-wrap' }}>{version.jd_text_norm}</p></details>)}
      <LifecycleControls entity={entity} id={id} showHistory /></div>}
  </Modal>
}

export function ArchivePage() {
  const [collection, setCollection] = useState<Collection>('demands')
  const [filters, setFilters] = useState({ q: '', grade: '', account: '', location: '' })
  const [page, setPage] = useState(1)
  const [open, setOpen] = useState<{ entity: Entity; id: number } | null>(null)
  const search = useQuery({ queryKey: ['archive', collection, filters, page], queryFn: () => api.maintenance.search(collection, { ...filters, page, page_size: 25 }) })
  const exportCsv = useMutation({ mutationFn: () => api.maintenance.export(collection, filters), onSuccess: (blob) => downloadBlob(blob, `archived-${collection}.csv`) })
  return <div className="page stack">
    <div className="page-header"><h1>Archive</h1><span className="meta">Read-only records · Contact details masked by default</span><span className="spacer" /><button className="btn" disabled={exportCsv.isPending} onClick={() => exportCsv.mutate()}>Export CSV</button></div>
    <div className="card stack">
      <div className="tabs" role="tablist" aria-label="Archived entities">{(['demands', 'resources'] as const).map((value) => <button key={value} role="tab" aria-selected={collection === value} onClick={() => { setCollection(value); setPage(1) }}>{value === 'demands' ? 'Demands' : 'Resources'}</button>)}</div>
      <div className="row">{([['q', 'Search archive'], ['grade', 'Grade'], ['account', collection === 'demands' ? 'Account' : 'Group'], ['location', 'Location']] as const).map(([key, label]) => <label key={key} className="field">{label}<input value={filters[key]} placeholder={key === 'q' ? 'Requisition, account, role or name…' : undefined} onChange={(e) => { setFilters({ ...filters, [key]: e.target.value }); setPage(1) }} /></label>)}</div>
    </div>
    {search.isLoading && <Spinner />}{search.isError && <ErrorState error={search.error} onRetry={() => void search.refetch()} />}{exportCsv.isError && <ErrorState error={exportCsv.error} />}
    {search.data && <>
      <div className="table-wrap"><table className="data" aria-label="Archived records"><thead><tr><th>Record</th><th>{collection === 'demands' ? 'Account / role' : 'Email'}</th><th>Grade</th><th>Location</th><th>Archived on</th><th>Detail</th></tr></thead><tbody>
        {search.data.items.map((row) => <tr key={row.demand_id ?? row.resource_id}>
          <td>{row.code_requisition ?? row.short_name}</td>
          <td>{collection === 'demands' ? <>{row.account_name}<div className="small muted">{row.demand_request_name}</div></> : <MaskedValue key={row.resource_id} masked={row.email_address ?? null} entity="resource" id={row.resource_id!} />}</td>
          <td>{row.code_local_grade ?? row.grade ?? '—'}</td><td>{row.region ?? row.location ?? '—'}</td><td>{formatDate(row.archived_at)}</td>
          <td><button className="btn btn-sm" onClick={() => setOpen({ entity: collection === 'demands' ? 'demand' : 'resource', id: (row.demand_id ?? row.resource_id)! })}>Read-only detail</button></td>
        </tr>)}
      </tbody></table></div>
      {search.data.total === 0 && <p className="card muted">No archived records match your search.</p>}
      <div className="row"><span>{countLabel(search.data.total, 'record')} · Page {page}</span><button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button><button className="btn" disabled={page * 25 >= search.data.total} onClick={() => setPage(page + 1)}>Next</button></div>
    </>}
    {open && <ArchivedDetail {...open} onClose={() => setOpen(null)} />}
  </div>
}
