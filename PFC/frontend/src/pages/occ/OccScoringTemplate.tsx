import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { ScoringTemplate, ScoringWeights } from '../../api/types'
import { Banner, Chip, ErrorState, Spinner } from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { weightTotal } from '../../lib/occ'

const COMPONENTS: [keyof ScoringWeights, string][] = [
  ['skills', 'Skills, experience and domain'],
  ['grade', 'Grade alignment'],
  ['location', 'Location alignment'],
  ['benchStart', 'Bench Start Date'],
  ['benchEnd', 'Bench End Date'],
  ['status', 'Bench Status'],
]

/** Versioned scoring template (REQ-SCR-01/02, REQ-ROLE-04, GUI_OCC_DESIGN.md §16.6). */
export function OccScoringTemplate() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['occ', 'templates'], queryFn: () => api.listTemplates() })
  const [draft, setDraft] = useState<ScoringTemplate | null>(null)
  const publish = useMutation({
    mutationFn: (t: ScoringTemplate) =>
      api.publishTemplate({ weights: t.weights, adjacentGradePoints: t.adjacentGradePoints, statusScoreMode: t.statusScoreMode, bands: t.bands, changeNote: t.changeNote }),
    onSuccess: () => {
      setDraft(null)
      void qc.invalidateQueries()
    },
  })
  if (list.isLoading) return <Spinner />
  if (list.isError) return <ErrorState error={list.error} />
  const active = list.data?.find((t) => t.status === 'active')
  if (!active) return <Banner tone="danger">No active scoring template.</Banner>
  const t = draft ?? { ...active, changeNote: '' }
  const total = weightTotal(t.weights)
  const bandsOk = t.bands.strong > t.bands.potential && t.bands.potential > t.bands.avg && t.bands.avg > 0
  const setW = (k: keyof ScoringWeights, v: number) => setDraft({ ...t, weights: { ...t.weights, [k]: v } })
  const setB = (k: keyof ScoringTemplate['bands'], v: number) => setDraft({ ...t, bands: { ...t.bands, [k]: v } })
  return (
    <div className="stack">
      <section className="card stack" aria-label="Scoring template">
        <div className="row">
          <h2>Scoring template</h2>
          <span className="spacer" />
          <Chip tone="success">
            Active: v{active.version} (since {formatDateTime(active.createdAt)})
          </Chip>
        </div>
        <span className="muted small">Editing draft v{Math.max(...(list.data ?? []).map((x) => x.version)) + 1}</span>
        <table className="data">
          <thead>
            <tr>
              <th>Component</th>
              <th>Weight</th>
              <th>Rule</th>
            </tr>
          </thead>
          <tbody>
            {COMPONENTS.map(([k, label]) => (
              <tr key={k}>
                <td>{label}</td>
                <td>
                  <input type="number" min={0} max={100} aria-label={`${label} weight`} value={t.weights[k]} onChange={(e) => setW(k, Number(e.target.value))} style={{ width: 72 }} />
                </td>
                <td className="small">
                  {k === 'grade' && (
                    <>
                      Exact full · ±1 grade{' '}
                      <input type="number" min={0} aria-label="Adjacent grade points" value={t.adjacentGradePoints} onChange={(e) => setDraft({ ...t, adjacentGradePoints: Number(e.target.value) })} style={{ width: 56 }} /> · &gt;1
                      excluded (fixed)
                    </>
                  )}
                  {k === 'status' && (
                    <label className="row">
                      status_score_mode
                      <select value={t.statusScoreMode} onChange={(e) => setDraft({ ...t, statusScoreMode: e.target.value as ScoringTemplate['statusScoreMode'] })}>
                        <option value="binary">binary</option>
                        <option value="tiered">tiered</option>
                      </select>
                    </label>
                  )}
                  {k === 'benchEnd' && 'Full when blank'}
                  {k === 'benchStart' && 'Full when available ≤ Position Start'}
                  {k === 'location' && 'Aligned / Remote 5 · Potential travel 3 · Location check 1'}
                  {k === 'skills' && 'Mandatory / preferred / semantic split'}
                </td>
              </tr>
            ))}
            <tr>
              <td>Available Capacity · Tracker · Group</td>
              <td>0</td>
              <td className="small">Not scored — locked (REQ-ELIG-06)</td>
            </tr>
            <tr>
              <th style={{ position: 'static' }}>Total</th>
              <th style={{ position: 'static' }} className="num" data-testid="weight-total">
                {total} {total === 100 ? '✓' : '✕'}
              </th>
              <th style={{ position: 'static' }} />
            </tr>
          </tbody>
        </table>
        {total !== 100 && <Banner tone="danger">Weights must total 100 (currently {total}).</Banner>}
        <div className="row small">
          Bands: Strong ≥ <input type="number" aria-label="Strong threshold" value={t.bands.strong} onChange={(e) => setB('strong', Number(e.target.value))} style={{ width: 60 }} /> · Potential ≥{' '}
          <input type="number" aria-label="Potential threshold" value={t.bands.potential} onChange={(e) => setB('potential', Number(e.target.value))} style={{ width: 60 }} /> · Avg. ≥{' '}
          <input type="number" aria-label="Avg threshold" value={t.bands.avg} onChange={(e) => setB('avg', Number(e.target.value))} style={{ width: 60 }} /> · Weak below · Insufficient data
        </div>
        {!bandsOk && <Banner tone="danger">Band thresholds must be descending.</Banner>}
        <label className="field">
          Change note (required)
          <input value={t.changeNote} onChange={(e) => setDraft({ ...t, changeNote: e.target.value })} />
        </label>
        <div className="modal-actions">
          <button className="btn" disabled={!draft} onClick={() => setDraft(null)}>
            Discard draft
          </button>
          <button className="btn btn-primary" disabled={!draft || total !== 100 || !bandsOk || !t.changeNote.trim() || publish.isPending} onClick={() => publish.mutate(t)}>
            Publish v{Math.max(...(list.data ?? []).map((x) => x.version)) + 1}
          </button>
        </div>
        {publish.isError && <ErrorState error={publish.error} />}
      </section>
      <section className="card" aria-label="Template history">
        <h3>History</h3>
        <table className="data">
          <thead>
            <tr>
              <th>Version</th>
              <th>Status</th>
              <th>Weights</th>
              <th>Note</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {(list.data ?? []).map((x) => (
              <tr key={x.templateId}>
                <td>v{x.version}</td>
                <td>
                  <Chip tone={x.status === 'active' ? 'success' : 'neutral'}>{x.status}</Chip>
                </td>
                <td className="mono small">{COMPONENTS.map(([k]) => x.weights[k]).join(' / ')}</td>
                <td>{x.changeNote}</td>
                <td>
                  {formatDateTime(x.createdAt)} · {x.createdBy}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
