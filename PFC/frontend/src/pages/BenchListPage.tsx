import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Info, RefreshCw, Upload } from 'lucide-react'
import { api } from '../api'
import type { Resource } from '../api/types'
import { Chip, EligibilityBadge, EmptyState, ErrorState, MaskedValue, Modal, Spinner } from '../components/ui'
import { UploadDialog } from '../components/UploadDialog'
import { formatDate, orNotRecorded } from '../lib/format'

type View = 'eligible' | 'excluded' | 'all'

const isEligible = (r: Resource) => r.eligibility === 'eligible' || r.eligibility === 'eligible_caveat'

/** Bench List (REQ-IN-10..14, REQ-IN-20..22, REQ-ELIG-01..08, GUI_OCC_DESIGN.md §10). */
export function BenchListPage() {
  const qc = useQueryClient()
  // CV sync is a background job with no status route, so refresh the list for a while after submitting it.
  const [pollUntil, setPollUntil] = useState(0)
  const resources = useQuery({
    queryKey: ['resources'],
    queryFn: () => api.listResources(),
    refetchInterval: () => (Date.now() < pollUntil ? 3000 : false),
  })
  const [view, setView] = useState<View>('eligible')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [confirm, setConfirm] = useState(false)
  const [upload, setUpload] = useState(false)
  const sync = useMutation({
    mutationFn: (ids: number[]) => api.syncCvs(ids),
    onSuccess: () => {
      setSelected(new Set())
      setPollUntil(Date.now() + 60_000)
      void qc.invalidateQueries({ queryKey: ['resources'] })
    },
  })
  const all = useMemo(() => resources.data ?? [], [resources.data])
  const counts = { eligible: all.filter(isEligible).length, excluded: all.filter((r) => !isEligible(r)).length, all: all.length }
  const rows = all
    .filter((r) => (view === 'all' ? true : view === 'eligible' ? isEligible(r) : !isEligible(r)))
    .filter((r) => [r.shortName, r.name, r.location, r.benchStatus, r.skills.join(' ')].some((v) => (v ?? '').toLowerCase().includes(search.toLowerCase())))
    .sort((a, b) => (a.benchStatusPriority ?? 99) - (b.benchStatusPriority ?? 99))
  const toggle = (id: number) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }
  if (resources.isLoading) return <div className="page"><Spinner /></div>
  if (resources.isError) return <div className="page"><ErrorState error={resources.error} onRetry={() => void resources.refetch()} /></div>
  return (
    <div className="page">
      <div className="page-header">
        <h1>Bench List</h1>
        <span className="meta">Last upload: CT_Front_Sheet.xlsx · 04 Nov 2026 09:20 · Resume folder ● reachable</span>
        <span className="spacer" />
        <Chip>Auto-sync: Not configured</Chip>
        <button className="btn" disabled={selected.size === 0} title={selected.size ? undefined : 'Select at least one candidate'} onClick={() => setConfirm(true)}>
          <RefreshCw size={16} /> Sync selected ({selected.size})
        </button>
        <button className="btn btn-primary" onClick={() => setUpload(true)}>
          <Upload size={16} /> Upload CT
        </button>
      </div>
      <div className="card row">
        <div className="tabs" role="tablist" aria-label="View">
          {(['eligible', 'excluded', 'all'] as View[]).map((v) => (
            <button key={v} role="tab" aria-selected={view === v} onClick={() => setView(v)}>
              {v === 'eligible' ? 'Eligible' : v === 'excluded' ? 'Excluded from matching' : 'All'} ({counts[v]})
            </button>
          ))}
        </div>
        <span className="spacer" />
        <input aria-label="Search bench" placeholder="Search…" value={search} onChange={(e) => setSearch(e.target.value)} />
      </div>
      {rows.length === 0 ? (
        <EmptyState message="No bench data yet. Upload the Candidate Tracker (.xlsx)." />
      ) : (
        <div className="table-wrap">
          <table className="data" aria-label="Bench">
            <thead>
              <tr>
                <th>
                  <span className="sr-only">Select</span>
                </th>
                <th>ShortName</th>
                <th>Name</th>
                <th>Email Address</th>
                <th>Grade</th>
                <th>Location</th>
                <th>Bench Status</th>
                <th>Eligibility</th>
                <th title="Indicates capacity for business-development activities. Not used to determine availability.">
                  Avail. Cap. <Info size={12} aria-label="Display only — not used to determine availability" />
                </th>
                <th>Bench Start</th>
                <th>Group</th>
                <th>Skills</th>
                <th>CV status</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.resourceId} className={selected.has(r.resourceId) ? 'selected' : undefined}>
                  <td>
                    <input type="checkbox" checked={selected.has(r.resourceId)} onChange={() => toggle(r.resourceId)} aria-label={`Select ${r.shortName}`} />
                  </td>
                  <td className="mono">{r.candidateId ? <Link to={`/candidates/${r.candidateId}`}>{r.shortName}</Link> : r.shortName}</td>
                  <td>
                    {orNotRecorded(r.name)}
                    {r.identityStatus !== 'complete' && <Chip tone="warning">Incomplete identity</Chip>}
                  </td>
                  <td>
                    <MaskedValue masked={r.emailAddressMasked} entity="resource" id={r.resourceId} />
                  </td>
                  <td>{orNotRecorded(r.grade)}</td>
                  <td>{r.location}</td>
                  <td>
                    {r.benchStatus} {r.unmappedStatus && <Chip tone="warning">Unmapped status</Chip>}
                  </td>
                  <td>
                    <EligibilityBadge eligibility={r.eligibility} reasons={r.exclusionReasons} />
                    {r.exclusionReasons.length > 0 && <div className="small muted">{r.exclusionReasons.join('; ')}</div>}
                  </td>
                  <td>{r.availableCapacity ?? '—'}</td>
                  <td>{formatDate(r.benchStartDate)}</td>
                  <td>{r.groupName}</td>
                  <td className="small">{r.skills.join(', ')}</td>
                  <td>
                    <Chip tone={r.cvStatus === 'Synced' ? 'success' : r.cvStatus === 'Not synced' ? 'neutral' : 'warning'}>
                      {r.cvStatus}
                      {r.cvStatus === 'Synced' ? ` (${r.cvCount})` : ''}
                    </Chip>
                  </td>
                  <td>
                    <button className="btn btn-sm" onClick={() => sync.mutate([r.resourceId])} disabled={sync.isPending}>
                      <RefreshCw size={12} /> Sync
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {confirm && (
        <Modal
          title={`Sync ${selected.size} candidate(s)?`}
          onClose={() => setConfirm(false)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirm(false)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                onClick={() => {
                  sync.mutate([...selected])
                  setConfirm(false)
                }}
              >
                Sync
              </button>
            </>
          }
        >
          <p>CVs are searched in the configured resume folder. A missing CV is not a blocker — matching continues on CT skills.</p>
        </Modal>
      )}
      {upload && <UploadDialog kind="ct" onClose={() => setUpload(false)} />}
    </div>
  )
}
