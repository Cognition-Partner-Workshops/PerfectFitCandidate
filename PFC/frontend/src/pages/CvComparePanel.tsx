import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../api'
import type { CompareFact, CompareListItem, CompareStatus, CompareValue, CompareVersionScore, CvComparison, DiffRow } from '../api/types'
import { Banner, Chip, ErrorState, Spinner } from '../components/ui'
import { formatDate } from '../lib/format'
import { useTasks } from '../lib/tasks'

const TONE = { added: 'success', removed: 'danger', changed: 'warning', unchanged: 'neutral' } as const
const LABEL: Record<string, string> = { name: 'Name', phone: 'Phone', years_of_experience: 'Years of experience', degree: 'Degree' }

function StatusChip({ status }: { status: CompareStatus }) {
  return <Chip tone={TONE[status]}>{status[0].toUpperCase() + status.slice(1)}</Chip>
}

function Value({ value }: { value: CompareValue | null }) {
  if (!value) return <span className="muted">Not found in CV</span>
  return (
    <div className="stack">
      <span>{value.value}{value.raw && value.raw !== value.value ? <span className="small muted"> (CV: {value.raw})</span> : null}</span>
      {value.evidence && (
        <details>
          <summary className="small">Evidence{value.source ? ` · ${value.source}` : ''}</summary>
          <q className="small">{value.evidence}</q>
        </details>
      )}
    </div>
  )
}

function FactTable({ title, rows, left, right }: { title: string; rows: (CompareFact | CompareListItem)[]; left: string; right: string }) {
  const counts = (['added', 'removed', 'unchanged'] as const).map((s) => `${rows.filter((r) => r.status === s).length} ${s}`).join(' · ')
  return (
    <section className="card stack" aria-label={title}>
      <h3>{title} <span className="small muted">{rows.length ? counts : ''}</span></h3>
      {rows.length === 0 ? <p className="muted">Not found in either CV version.</p> : (
        <table className="data compare-table">
          <thead><tr><th>{title === 'Facts' ? 'Fact' : 'Item'}</th><th>{left}</th><th>{right}</th><th>Change</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className={`compare-${r.status}`}>
                <td>{'label' in r ? r.label : LABEL[r.key] ?? r.key}</td>
                <td><Value value={r.left} /></td>
                <td><Value value={r.right} /></td>
                <td><StatusChip status={r.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function TextDiff({ diff, left, right }: { diff: CvComparison['textDiff']; left: string; right: string }) {
  const cell = (line: DiffRow['left']) => line ? <><span className="mono small muted">{line.no}</span> {line.text}</> : null
  return (
    <section className="card stack" aria-label="Text differences">
      <h3>Text differences <span className="small muted">+{diff.added} / −{diff.removed} lines · {diff.unchanged} unchanged</span></h3>
      <p className="small muted">Normalised CV text, line by line. Emails and phone numbers are masked.</p>
      {diff.rows.length === 0 ? <p className="muted">No CV text to compare.</p> : (
        <table className="data diff-table">
          <thead><tr><th>{left}</th><th>{right}</th></tr></thead>
          <tbody>
            {diff.rows.map((row, i) => row.op === 'skip' ? (
              <tr key={i} className="diff-skip"><td colSpan={2} className="small muted">… {row.count} unchanged lines …</td></tr>
            ) : (
              <tr key={i} className={`diff-${row.op}`}>
                <td className={row.op === 'removed' || row.op === 'changed' ? 'diff-old' : undefined}>{cell(row.left)}</td>
                <td className={row.op === 'added' || row.op === 'changed' ? 'diff-new' : undefined}>{cell(row.right)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {diff.truncated && <Banner tone="info">Only the first 2,000 diff rows are shown.</Banner>}
    </section>
  )
}

const score = (v: CompareVersionScore) => v.excluded ? <Chip tone="neutral" title={v.reasons.map((r) => r.detail).join('; ')}>Excluded</Chip> : <span>{v.score?.toFixed(1)} <span className="small muted">{v.band}</span></span>
const signed = (n: number) => `${n > 0 ? '+' : ''}${Number(n.toFixed(2))}`

function Breakdown({ left, right }: { left: CompareVersionScore; right: CompareVersionScore }) {
  const names = [...new Set([...left.components, ...right.components].map((c) => c.component))]
  const find = (v: CompareVersionScore, n: string) => v.components.find((c) => c.component === n)
  return (
    <details>
      <summary className="small">Score breakdown</summary>
      <table className="data">
        <thead><tr><th>Component</th><th>Left</th><th>Right</th><th>Δ</th></tr></thead>
        <tbody>
          {names.map((n) => {
            const [l, r] = [find(left, n), find(right, n)]
            return (
              <tr key={n}>
                <td>{n.replace('_', ' ')}</td>
                <td>{l ? `${l.points} / ${l.maxPoints}` : '—'}</td>
                <td>{r ? `${r.points} / ${r.maxPoints}` : '—'}</td>
                <td>{l && r ? signed(r.points - l.points) : '—'}</td>
              </tr>
            )
          })}
          <tr><td>Mandatory missing</td><td>{left.mandatoryMissing}</td><td>{right.mandatoryMissing}</td><td>{signed(right.mandatoryMissing - left.mandatoryMissing)}</td></tr>
        </tbody>
      </table>
      {(left.gaps.length > 0 || right.gaps.length > 0) && (
        <p className="small">Gaps — left: {left.gaps.join('; ') || 'none'} · right: {right.gaps.join('; ') || 'none'}</p>
      )}
    </details>
  )
}

function Scores({ candidateId, data, left, right }: { candidateId: number; data: CvComparison; left: string; right: string }) {
  const { addJob } = useTasks()
  const [l, r] = [data.left.cvVersionId, data.right.cvVersionId]
  const latest = useQuery({
    queryKey: ['cv-compare-scoring', candidateId, l, r],
    queryFn: () => api.getLatestCvComparison(candidateId, l, r),
    refetchInterval: (q) => (q.state.data?.status === 'queued' || q.state.data?.status === 'running' ? 1500 : false),
  })
  const start = useMutation({
    mutationFn: () => api.startCvComparison(candidateId, l, r),
    onSuccess: ({ jobId }) => {
      addJob(jobId, `CV comparison scoring CV-${l} vs CV-${r}`)
      void latest.refetch()
    },
  })
  const removed = data.left.extractionRemoved || data.right.extractionRemoved
  const scoring = latest.data
  const busy = scoring?.status === 'queued' || scoring?.status === 'running'
  return (
    <section className="card stack" aria-label="Score differences">
      <h3>Scores for current demands</h3>
      {data.storedScores.length === 0 ? <p className="muted">No current match results for this candidate. Run matching first.</p> : (
        <table className="data">
          <thead><tr><th>Requisition</th><th>Role</th><th>Ranked score</th><th>{left} (stored)</th><th>{right} (stored)</th></tr></thead>
          <tbody>
            {data.storedScores.map((s) => (
              <tr key={s.resultId}>
                <td className="mono">{s.codeRequisition}</td>
                <td>{s.role ?? '—'}</td>
                <td>{s.score.toFixed(1)} <span className="small muted">{s.band}{s.bestCvVersionId ? ` · CV-${s.bestCvVersionId}` : ' · CT only'}</span></td>
                <td>{s.left ? `${s.left.score.toFixed(1)} ${s.left.band}` : <span className="muted">Not scored</span>}</td>
                <td>{s.right ? `${s.right.score.toFixed(1)} ${s.right.band}` : <span className="muted">Not scored</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="small muted">Matching scores only the latest CV version. Score both versions to see what each would score as the latest CV with the active template and each demand's run date. This is read-only: match results, rankings and emails do not change.</p>
      <div className="row">
        <button className="btn" disabled={removed || start.isPending || busy || data.storedScores.length === 0} onClick={() => start.mutate()}>
          {scoring ? 'Re-score both versions' : 'Score both versions'}
        </button>
        {removed && <span className="small muted">A removed CV extraction cannot be scored.</span>}
        {busy && <Spinner />}
      </div>
      {start.isError && <ErrorState error={start.error} />}
      {scoring?.status === 'failed' && <Banner tone="danger">Scoring failed. Try again.</Banner>}
      {scoring?.status === 'cancelled' && <Banner tone="info">Scoring was cancelled.</Banner>}
      {scoring?.status === 'completed' && scoring.result && (
        <>
          <span className="small muted">Scored {formatDate(scoring.finishedAt ?? scoring.createdAt)} · template {scoring.result.templateName} v{scoring.result.templateVersion}</span>
          <table className="data" aria-label="What-if scores">
            <thead><tr><th>Requisition</th><th>{left}</th><th>{right}</th><th>Δ</th><th>Breakdown</th></tr></thead>
            <tbody>
              {scoring.result.demands.map((d) => (
                <tr key={`${d.demandId}-${d.resourceId}`}>
                  <td className="mono">{d.codeRequisition}<div className="small muted">{d.role ?? ''}</div></td>
                  <td>{score(d.left)}</td>
                  <td>{score(d.right)}</td>
                  <td>{d.delta === null ? '—' : <Chip tone={d.delta > 0 ? 'success' : d.delta < 0 ? 'danger' : 'neutral'}>{signed(d.delta)}</Chip>}</td>
                  <td><Breakdown left={d.left} right={d.right} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}

/** CV version comparison (REQ-IN-24, FDD FR-ING-52, GUI_OCC_DESIGN.md §CV versions): read-only and masked. */
export function CvComparePanel({ candidateId, left, right, onClose }: { candidateId: number; left: number; right: number; onClose: () => void }) {
  const q = useQuery({ queryKey: ['cv-compare', candidateId, left, right], queryFn: () => api.compareCvVersions(candidateId, left, right) })
  if (q.isLoading) return <Spinner />
  if (q.isError || !q.data) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  const d = q.data
  const name = (v: CvComparison['left']) => `CV-${v.cvVersionId} (v${v.versionNo}${v.isLatest ? ', latest' : ''})`
  const [ln, rn] = [name(d.left), name(d.right)]
  return (
    <div className="stack" role="region" aria-label="CV version comparison">
      <section className="card stack">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h3>Compare {ln} ↔ {rn}</h3>
          <button className="btn" onClick={onClose}>Close comparison</button>
        </div>
        <div className="row"><Chip tone="info">Read-only</Chip><Chip tone="neutral">PII masked</Chip></div>
        <table className="data">
          <thead><tr><th /><th>{ln}</th><th>{rn}</th></tr></thead>
          <tbody>
            <tr><td>File</td><td>{d.left.fileName}</td><td>{d.right.fileName}</td></tr>
            <tr><td>Modified</td><td>{formatDate(d.left.modifiedAt)}</td><td>{formatDate(d.right.modifiedAt)}</td></tr>
            <tr><td>Extracted</td><td>{formatDate(d.left.extractedAt)}</td><td>{formatDate(d.right.extractedAt)}</td></tr>
          </tbody>
        </table>
        {(d.left.extractionRemoved || d.right.extractionRemoved) && <Banner tone="info">An extraction was removed by OCC Admin; its facts and text are not shown.</Banner>}
        {d.facts.yearsDelta !== null && d.facts.yearsDelta !== 0 && <span className="small">Years of experience change: {signed(d.facts.yearsDelta)}</span>}
      </section>
      <FactTable title="Facts" rows={d.facts.summary} left={ln} right={rn} />
      <FactTable title="Skills" rows={d.facts.skills} left={ln} right={rn} />
      {[['Experience', d.facts.experience], ['Qualifications', d.facts.qualifications], ['Projects', d.facts.projects]].map(([title, rows]) =>
        (rows as CompareListItem[]).length ? <FactTable key={title as string} title={title as string} rows={rows as CompareListItem[]} left={ln} right={rn} /> : null)}
      <TextDiff diff={d.textDiff} left={ln} right={rn} />
      <Scores candidateId={candidateId} data={d} left={ln} right={rn} />
    </div>
  )
}
