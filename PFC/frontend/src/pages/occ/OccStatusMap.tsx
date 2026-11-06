import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp } from 'lucide-react'
import { api } from '../../api'
import type { BenchStatus } from '../../api/types'
import { Banner, Chip, ErrorState, Spinner } from '../../components/ui'

/** Status list, priority and source-status mapping (REQ-ELIG-02/03/07, GUI_OCC_DESIGN.md §16.7). */
export function OccStatusMap() {
  const qc = useQueryClient()
  const statuses = useQuery({ queryKey: ['occ', 'statuses'], queryFn: () => api.listBenchStatuses() })
  const map = useQuery({ queryKey: ['occ', 'status-map'], queryFn: () => api.getStatusMap() })
  const [draft, setDraft] = useState<BenchStatus[] | null>(null)
  const [mapTo, setMapTo] = useState<Record<string, string>>({})
  const list = draft ?? statuses.data ?? []
  const saveStatuses = useMutation({ mutationFn: (s: BenchStatus[]) => api.saveBenchStatuses(s), onSuccess: () => { setDraft(null); void qc.invalidateQueries() } })
  const saveMap = useMutation({ mutationFn: api.saveStatusMap.bind(api), onSuccess: () => void qc.invalidateQueries() })
  if (statuses.isLoading || map.isLoading) return <Spinner />
  if (statuses.isError) return <ErrorState error={statuses.error} />
  const eligible = list.filter((s) => s.eligibility !== 'Excluded')
  const move = (code: string, dir: -1 | 1) => {
    const order = [...eligible]
    const i = order.findIndex((s) => s.code === code)
    const j = i + dir
    if (j < 0 || j >= order.length) return
    ;[order[i], order[j]] = [order[j] as BenchStatus, order[i] as BenchStatus]
    const renumbered = order.map((s, idx) => ({ ...s, priority: idx + 1 }))
    setDraft([...renumbered, ...list.filter((s) => s.eligibility === 'Excluded')])
  }
  return (
    <div className="stack">
      {map.data && map.data.unmapped.length > 0 && (
        <section className="card stack" aria-label="Unmapped statuses">
          <Banner tone="warning">
            {map.data.unmapped.length} unmapped status(es) in the latest CT. Until mapped they stay eligible at priority 2 with binary status score 0 and are flagged “Unmapped status”.
          </Banner>
          {map.data.unmapped.map((u) => (
            <div key={u} className="row">
              <span className="mono">{u}</span> →
              <select aria-label={`Map ${u}`} value={mapTo[u] ?? ''} onChange={(e) => setMapTo({ ...mapTo, [u]: e.target.value })}>
                <option value="">Choose canonical status…</option>
                {list.map((s) => (
                  <option key={s.code} value={s.code}>
                    {s.label}
                  </option>
                ))}
              </select>
              <button
                className="btn btn-sm"
                disabled={!mapTo[u]}
                onClick={() => saveMap.mutate([...(map.data?.entries ?? []), { sourceText: u, canonicalCode: mapTo[u] ?? '' }])}
              >
                Add to map
              </button>
            </div>
          ))}
        </section>
      )}
      <section className="card stack" aria-label="Status list and priority">
        <div className="row">
          <h2>Status list &amp; priority</h2>
          <span className="spacer" />
          <button className="btn" disabled={!draft} onClick={() => setDraft(null)}>
            Discard
          </button>
          <button className="btn btn-primary" disabled={!draft} onClick={() => draft && saveStatuses.mutate(draft)}>
            Save new version
          </button>
        </div>
        <table className="data">
          <thead>
            <tr>
              <th>Priority</th>
              <th>Status (exactly as in CT)</th>
              <th>Eligibility</th>
              <th>Tiered points</th>
              <th>Caveat text</th>
              <th>Order</th>
            </tr>
          </thead>
          <tbody>
            {[...eligible, ...list.filter((s) => s.eligibility === 'Excluded')].map((s) => (
              <tr key={s.code}>
                <td className="num">{s.priority ?? '—'}</td>
                <td>{s.label}</td>
                <td>
                  <Chip tone={s.eligibility === 'Excluded' ? 'danger' : s.eligibility === 'Eligible' ? 'success' : 'warning'}>{s.eligibility}</Chip>
                </td>
                <td className="num">{s.tieredPoints}</td>
                <td className="small">{s.caveat || '—'}</td>
                <td>
                  {s.eligibility !== 'Excluded' && (
                    <>
                      <button className="icon-btn" aria-label={`Move ${s.label} up`} onClick={() => move(s.code, -1)}>
                        <ArrowUp size={14} />
                      </button>
                      <button className="icon-btn" aria-label={`Move ${s.label} down`} onClick={() => move(s.code, 1)}>
                        <ArrowDown size={14} />
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <span className="small muted">Group “Not on Bench” and a parseable filled Bench End Date are fixed exclusion rules.</span>
      </section>
      <section className="card stack" aria-label="Source-status mapping">
        <h2>Source-status mapping</h2>
        <table className="data">
          <thead>
            <tr>
              <th>Uploaded text</th>
              <th>Canonical status</th>
            </tr>
          </thead>
          <tbody>
            {(map.data?.entries ?? []).map((m) => (
              <tr key={m.sourceText}>
                <td className="mono small">{m.sourceText}</td>
                <td>{list.find((s) => s.code === m.canonicalCode)?.label ?? m.canonicalCode}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
