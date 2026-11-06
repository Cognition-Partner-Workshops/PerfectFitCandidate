import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { Grade } from '../../api/types'
import { Banner, Chip, ErrorState, Spinner } from '../../components/ui'
import { gradeWindow, moveItem } from '../../lib/occ'

/** Ordered grade list driving ±1 grade alignment (REQ-GRD-01, GUI_OCC_DESIGN.md §16.8). */
export function OccGrades() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['occ', 'grades'], queryFn: () => api.listGrades() })
  const [draft, setDraft] = useState<Grade[] | null>(null)
  const [newCode, setNewCode] = useState('')
  const [previewCode, setPreviewCode] = useState('D1')
  const save = useMutation({
    mutationFn: (g: Grade[]) => api.saveGrades(g),
    onSuccess: (g) => {
      qc.setQueryData(['occ', 'grades'], g)
      setDraft(null)
    },
  })
  if (q.isLoading) return <Spinner />
  if (q.isError) return <ErrorState error={q.error} />
  const grades = draft ?? q.data ?? []
  const set = (g: Grade[]) => setDraft(g)
  const code = newCode.trim().toUpperCase()
  const codeOk = /^[A-Z]\d$/.test(code) && !grades.some((g) => g.code === code)
  const alignment = gradeWindow(grades, previewCode)
  return (
    <section className="card stack" aria-label="Grade list">
      <h2>Grade list</h2>
      <p className="meta">Lowest to highest. Grade alignment accepts the demand grade and one grade either side.</p>
      <ol className="stack" style={{ gap: 4, paddingLeft: 0, listStyle: 'none' }} aria-label="Ordered grades">
        {grades.map((g, i) => (
          <li key={g.code} className="row" style={{ gap: 8 }}>
            <span className="mono" style={{ width: 28 }}>{i + 1}.</span>
            <strong style={{ width: 40 }}>{g.code}</strong>
            {g.retired && <Chip tone="neutral">Retired</Chip>}
            <button className="btn btn-sm" aria-label={`Move ${g.code} up`} disabled={i === 0} onClick={() => set(moveItem(grades, i, i - 1))}>
              ↑
            </button>
            <button className="btn btn-sm" aria-label={`Move ${g.code} down`} disabled={i === grades.length - 1} onClick={() => set(moveItem(grades, i, i + 1))}>
              ↓
            </button>
            <button className="btn btn-sm" aria-label={`${g.retired ? 'Restore' : 'Retire'} ${g.code}`} onClick={() => set(grades.map((x) => (x.code === g.code ? { ...x, retired: !x.retired } : x)))}>
              {g.retired ? 'Restore' : 'Retire'}
            </button>
          </li>
        ))}
      </ol>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <label className="field">
          New grade
          <input value={newCode} onChange={(e) => setNewCode(e.target.value)} placeholder="e.g. E2" maxLength={2} />
        </label>
        <button
          className="btn"
          disabled={!codeOk}
          onClick={() => {
            set([...grades, { code, order: grades.length + 1, retired: false }])
            setNewCode('')
          }}
        >
          Add grade
        </button>
      </div>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <label className="field">
          Preview demand grade
          <select value={previewCode} onChange={(e) => setPreviewCode(e.target.value)}>
            {grades.filter((g) => !g.retired).map((g) => (
              <option key={g.code}>{g.code}</option>
            ))}
          </select>
        </label>
        <span data-testid="grade-window">
          {previewCode} demand → {alignment.length ? alignment.join(', ') : 'no active grades'}
        </span>
      </div>
      {save.isError && <Banner tone="danger">{(save.error as Error).message}</Banner>}
      {save.isSuccess && <Banner tone="success">Grade list saved. Re-run matching to apply the new order.</Banner>}
      <div className="row">
        <button className="btn btn-primary" disabled={!draft} onClick={() => draft && save.mutate(draft)}>
          Save grade list
        </button>
        <button className="btn" disabled={!draft} onClick={() => setDraft(null)}>
          Discard changes
        </button>
      </div>
    </section>
  )
}
