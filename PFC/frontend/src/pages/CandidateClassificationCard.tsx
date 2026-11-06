import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { CandidateClassification, CandidateType } from '../api/types'
import { useAuth } from '../auth/context'
import { Banner, ErrorState } from '../components/ui'
import { formatDateTime } from '../lib/format'

export function CandidateClassificationCard({ classification: c }: { classification: CandidateClassification }) {
  const { user } = useAuth()
  const qc = useQueryClient()
  const [value, setValue] = useState<CandidateType>(c.classificationOverride ?? c.computedClassification)
  const [reason, setReason] = useState('')
  const [saved, setSaved] = useState(false)
  const change = useMutation({
    mutationFn: (classification: CandidateType | null) => api.setClassification(c.candidateId, { classification, reason: reason.trim() }),
    onSuccess: async () => {
      setReason('')
      setSaved(true)
      await qc.invalidateQueries()
    },
  })
  return (
    <section className="card stack" aria-label="Candidate classification">
      <h2>Classification</h2>
      <div>Effective classification: <strong>{c.effectiveClassification}</strong></div>
      <div>Computed classification: <strong>{c.computedClassification}</strong></div>
      <p className="small muted">Domain is a heuristic, not proof of employment.</p>
      {c.classificationOverride !== null && <div className="stack small">
        <div>Override: {c.classificationOverride}</div>
        <div>Reason: {c.overrideReason}</div>
        <div>Set by user #{c.overrideBy} · {c.overrideAt ? formatDateTime(c.overrideAt) : 'Not recorded'}</div>
      </div>}
      {saved && <Banner tone="success">Classification updated.</Banner>}
      {change.isError && <ErrorState error={change.error} />}
      {user?.role === 'Admin' && <>
        <label className="field">Override classification
          <select value={value} onChange={(e) => setValue(e.target.value as CandidateType)}>
            <option>Internal</option><option>External</option><option>Unknown</option>
          </select>
        </label>
        <label className="field">Classification reason (required for setting or clearing)
          <textarea required value={reason} onChange={(e) => { setReason(e.target.value); setSaved(false) }} />
        </label>
        <div className="row">
          <button className="btn btn-primary" disabled={!reason.trim() || change.isPending} onClick={() => change.mutate(value)}>Set override</button>
          <button className="btn" disabled={!reason.trim() || c.classificationOverride === null || change.isPending} onClick={() => change.mutate(null)}>Clear override</button>
        </div>
      </>}
    </section>
  )
}
