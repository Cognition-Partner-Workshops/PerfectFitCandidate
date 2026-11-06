import { Fragment, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Download, Mail, TriangleAlert } from 'lucide-react'
import { api } from '../api'
import type { Band, MatchResult, MatchRun, ResultFilters } from '../api/types'
import { FilterBar } from '../components/FilterBar'
import { BandChip, Banner, Chip, EmptyState, ErrorState, MaskedValue, MatchDoughnut, Modal, ScoreBreakdown, Spinner } from '../components/ui'
import { applyResultFilters, distinct } from '../lib/filters'
import { groupByRequisition, preselectForEmail } from '../lib/emailSelection'
import { DISCLAIMER, RESULT_COLUMNS, resultsToCsv } from '../lib/csv'
import { formatDate, formatDateTime, roleLocation } from '../lib/format'
import { BAND_ORDER, bandLabel } from '../lib/bands'
import { useAuth } from '../auth/context'
import { canEdit } from '../lib/permissions'

function OverrideDialog({ row, onClose }: { row: MatchResult; onClose: () => void }) {
  const qc = useQueryClient()
  const [action, setAction] = useState<'pin' | 'exclude' | 'adjust_band'>('pin')
  const [band, setBand] = useState<Band>(row.band)
  const [reason, setReason] = useState('')
  const m = useMutation({
    mutationFn: () => api.addOverride(row.resultId, { action, reason, newBand: action === 'adjust_band' ? band : undefined }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['results'] })
      onClose()
    },
  })
  return (
    <Modal
      title={`Override — ${row.personName ?? 'candidate'} · ${row.codeRequisition}`}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={!reason.trim() || m.isPending} onClick={() => m.mutate()}>
            Save override
          </button>
        </>
      }
    >
      <label className="field">
        Action
        <select value={action} onChange={(e) => setAction(e.target.value as typeof action)}>
          <option value="pin">Pin (keep in shortlist)</option>
          <option value="exclude">Exclude from this requisition</option>
          <option value="adjust_band">Adjust band</option>
        </select>
      </label>
      {action === 'adjust_band' && (
        <label className="field">
          New band
          <select value={band} onChange={(e) => setBand(e.target.value as Band)}>
            {BAND_ORDER.map((b) => (
              <option key={b} value={b}>
                {bandLabel(b)}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className="field">
        Reason (required)
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <p className="small muted">Overrides never change the computed score. This action is recorded in the audit log.</p>
      {m.isError && <ErrorState error={m.error} />}
    </Modal>
  )
}

function ResultRow({ r, selected, onToggle, onOverride, canOverride }: { r: MatchResult; selected: boolean; onToggle: () => void; onOverride: () => void; canOverride: boolean }) {
  const [open, setOpen] = useState(false)
  const band = r.override?.action === 'adjust_band' && r.override.newBand ? r.override.newBand : r.band
  return (
    <>
      <tr className={selected ? 'selected' : undefined}>
        <td>
          <input type="checkbox" checked={selected} onChange={onToggle} aria-label={`Select ${r.personName ?? 'candidate'} for ${r.codeRequisition}`} />
        </td>
        <td className="num">
          <button className="icon-btn" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-label="Evidence and gaps">
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
          #{r.rank}
        </td>
        <td className="mono">{r.codeRequisition}</td>
        <td>{formatDate(r.positionStartDate)}</td>
        <td>{r.accountName}</td>
        <td title={r.demandRequestName ?? ''} style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {r.demandRequestName}
        </td>
        <td>{r.demandGrade}</td>
        <td>{roleLocation(r.region)}</td>
        <td>
          <Link to={`/candidates/${r.candidateId}`}>{r.personName ?? 'Not recorded'}</Link>
          {r.incompleteIdentity && (
            <span title="Incomplete identity" aria-label="Incomplete identity">
              {' '}
              <TriangleAlert size={12} color="var(--color-warning)" />
            </span>
          )}
        </td>
        <td>
          <MaskedValue masked={r.personEmailMasked} entity="resource" id={r.resourceId} />
        </td>
        <td>
          {r.personGrade} {r.gradeRelation !== 'exact' && <Chip tone="warning">±1</Chip>}
        </td>
        <td>{r.personLocation}</td>
        <td>
          <MatchDoughnut score={r.score} band={band} />
        </td>
        <td>
          <BandChip band={band} />
          {r.override && (
            <Chip tone="info" title={`${r.override.action} by ${r.override.createdBy}: ${r.override.reason}`}>
              Overridden
            </Chip>
          )}
        </td>
        <td>
          <Chip tone={r.locationAssessment === 'Aligned' ? 'success' : r.locationAssessment === 'Location check' ? 'danger' : 'neutral'}>{r.locationAssessment}</Chip>
        </td>
        <td className="small">{r.matchingEvidence.slice(0, 2).join(', ')}{r.matchingEvidence.length > 2 ? '…' : ''}</td>
        <td className="small">{r.gapsChecks.slice(0, 2).join('; ')}{r.gapsChecks.length > 2 ? '…' : ''}</td>
        <td className="small">{r.eligibilityEvidence[0]}</td>
        <td>
          {canOverride && (
            <button className="btn btn-sm" onClick={onOverride}>
              Override
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td />
          <td colSpan={18}>
            <div className="grid-12" style={{ padding: 8 }}>
              <div className="span-4">
                <h3>Score breakdown</h3>
                <ScoreBreakdown breakdown={r.breakdown} />
              </div>
              <div className="span-4">
                <h3>Matching evidence</h3>
                <ul className="small">{r.matchingEvidence.map((e) => <li key={e}>{e}</li>)}</ul>
                <h3>Eligibility evidence</h3>
                <ul className="small">{r.eligibilityEvidence.map((e) => <li key={e}>{e}</li>)}</ul>
              </div>
              <div className="span-4">
                <h3>Gaps / Checks</h3>
                <ul className="small">
                  {r.gapsChecks.map((g) => (
                    <li key={g} style={g.startsWith('Mandatory gap') ? { color: 'var(--color-danger)', fontWeight: 600 } : undefined}>
                      {g}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

export const RUN_POLL_MS = 1500
const isActive = (r: MatchRun) => r.status === 'queued' || r.status === 'running'

/** Match Results grid (REQ-OUT-01, REQ-MAT-03/09/10, REQ-OUT-08/09, GUI_OCC_DESIGN.md §11). */
export function MatchResultsPage() {
  const { user } = useAuth()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const [filters, setFilters] = useState<ResultFilters>(() => (params.get('req') ? { codeRequisition: params.get('req') ?? undefined } : {}))
  const [grouped, setGrouped] = useState(true)
  const [overrideRow, setOverrideRow] = useState<MatchResult | null>(null)
  const qc = useQueryClient()
  const runs = useQuery({ queryKey: ['runs'], queryFn: () => api.listMatchRuns() })
  // Runs are newest first; a queued/running run has no results yet, so the last completed run stays on screen.
  const run = runs.data?.find((r) => r.status === 'completed')
  const newer = runs.data?.[0] && isActive(runs.data[0]) ? runs.data[0] : undefined
  const progress = useQuery({
    queryKey: ['run', newer?.runId],
    queryFn: () => api.getMatchRun(newer?.runId ?? 0),
    enabled: !!newer,
    refetchInterval: (q) => (q.state.data && !isActive(q.state.data) ? false : RUN_POLL_MS),
  })
  useEffect(() => {
    if (progress.data && !isActive(progress.data)) void qc.invalidateQueries({ queryKey: ['runs'] })
  }, [progress.data, qc])
  const results = useQuery({ queryKey: ['results', run?.runId], queryFn: () => api.getResults(run?.runId ?? 0), enabled: !!run })
  const [selection, setSelection] = useState<Set<number> | null>(null)
  const send = useMutation({
    mutationFn: (ids: number[]) => api.createDrafts(ids),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['emails'] })
      navigate('/email')
    },
  })

  const all = useMemo(() => results.data ?? [], [results.data])
  const rows = useMemo(() => applyResultFilters(all, filters), [all, filters])
  const selected = selection ?? preselectForEmail(all)
  const options = useMemo(
    () => ({
      accounts: distinct(all, (r) => r.accountName),
      bus: distinct(all, (r) => r.bu),
      grades: ['A3', 'A4', 'B1', 'B2', 'C1', 'C2', 'D1', 'D2', 'E1'],
      locations: distinct(all, (r) => r.personLocation),
      regions: distinct(all, (r) => r.region),
      statuses: distinct(all, (r) => r.benchStatus),
    }),
    [all],
  )
  const toggle = (id: number) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelection(next)
  }

  if (runs.isLoading || results.isLoading) return <div className="page"><Spinner /></div>
  if (runs.isError) return <div className="page"><ErrorState error={runs.error} /></div>
  if (!run) {
    return (
      <div className="page">
        {newer && (
          <Banner tone="info">
            Scoring run {newer.runId} is {progress.data?.status ?? newer.status}. {run ? 'Showing the last completed results until it finishes.' : 'Results appear when it finishes.'}
          </Banner>
        )}
        <EmptyState message="No match results yet. Analyze and confirm JDs in Demand List to start scoring." action={<Link className="btn btn-primary" to="/demands">Go to Demand List</Link>} />
      </div>
    )
  }
  const meta = `Run ${formatDateTime(run.finishedAt)} · Model ${run.llmModel} · Template v${run.templateVersion}`
  const exportCsv = () => {
    const blob = new Blob([resultsToCsv(rows, meta)], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `PFC-match-results-run${run.runId}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }
  const selectedVisible = rows.filter((r) => selected.has(r.resultId))
  const groups = grouped ? groupByRequisition(rows) : [['', rows] as [string, MatchResult[]]]
  const canOverride = !!user && canEdit(user.role, 'matchOverrides')

  return (
    <div className="page">
      {newer && (
        <Banner tone="info">
          Scoring run {newer.runId} is {progress.data?.status ?? newer.status}. {run ? 'Showing the last completed results until it finishes.' : 'Results appear when it finishes.'}
        </Banner>
      )}
      <div className="page-header">
        <h1>Match Results</h1>
        <span className="meta">
          {meta} {user?.role === 'Analyst' && '(read-only)'} · {run.demandsAssessed} reqs · {run.pairsScored} rows
        </span>
        <span className="spacer" />
        <button className="btn" onClick={exportCsv}>
          <Download size={16} /> Export CSV
        </button>
        <button className="btn btn-primary" disabled={selectedVisible.length === 0 || send.isPending} onClick={() => send.mutate(selectedVisible.map((r) => r.resultId))} title={selectedVisible.length ? undefined : 'Select at least one candidate'}>
          <Mail size={16} /> Send selected to Email Approval ({selectedVisible.length})
        </button>
      </div>
      <div className="card stack">
        <FilterBar value={filters} onChange={setFilters} options={options} splitGrade />
        <div className="row small">
          <span>Group:</span>
          <label className="row">
            <input type="radio" checked={grouped} onChange={() => setGrouped(true)} /> By requisition
          </label>
          <label className="row">
            <input type="radio" checked={!grouped} onChange={() => setGrouped(false)} /> Flat list
          </label>
          <span className="spacer" />
          <span className="muted">Top 4 of 5 per requisition are pre-selected for email.</span>
        </div>
      </div>
      {results.isError && <ErrorState error={results.error} />}
      <div className="table-wrap">
        <table className="data" aria-label="Match results">
          <thead>
            <tr>
              <th>
                <span className="sr-only">Select</span>
              </th>
              <th>#</th>
              {RESULT_COLUMNS.map((c) => (
                <th key={c}>{c}</th>
              ))}
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={19}>
                  <EmptyState message="No results match the current filters." />
                </td>
              </tr>
            )}
            {groups.map(([req, list]) => (
              <Fragment key={req || 'flat'}>
                {grouped && list[0] && (
                  <tr className="group-row">
                    <td colSpan={19}>
                      <span className="mono">{req}</span> · {list[0].demandRequestName} · {list[0].accountName} · {list[0].demandGrade} · {roleLocation(list[0].region)} · {formatDate(list[0].positionStartDate)}{' '}
                      {!list.some((r) => r.band !== 'Weak') && <Chip tone="danger">No suitable candidate — Weak Match shown</Chip>}
                    </td>
                  </tr>
                )}
                {list.map((r) => (
                  <ResultRow key={r.resultId} r={r} selected={selected.has(r.resultId)} onToggle={() => toggle(r.resultId)} onOverride={() => setOverrideRow(r)} canOverride={canOverride} />
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {send.isError && <Banner tone="danger">{(send.error as Error).message}</Banner>}
      <p className="disclaimer">{DISCLAIMER}</p>
      {overrideRow && <OverrideDialog row={overrideRow} onClose={() => setOverrideRow(null)} />}
    </div>
  )
}
