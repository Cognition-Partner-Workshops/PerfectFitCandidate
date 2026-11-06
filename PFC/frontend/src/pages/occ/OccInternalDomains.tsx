import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { InternalDomain } from '../../api/types'
import { Banner, Chip, ErrorState, Spinner } from '../../components/ui'
import { normaliseDomain } from '../../lib/occ'

/** Internal email domains (REQ-CLS-01/02, GUI_OCC_DESIGN.md §16.9). */
export function OccInternalDomains() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['occ', 'domains'], queryFn: () => api.listInternalDomains() })
  const [draft, setDraft] = useState<InternalDomain[] | null>(null)
  const [input, setInput] = useState('')
  const domains = draft ?? list.data ?? []
  const preview = useQuery({ queryKey: ['occ', 'domains-preview', domains], queryFn: () => api.previewClassification(domains), enabled: !!list.data })
  const save = useMutation({ mutationFn: (d: InternalDomain[]) => api.saveInternalDomains(d), onSuccess: () => { setDraft(null); void qc.invalidateQueries() } })
  if (list.isLoading) return <Spinner />
  if (list.isError) return <ErrorState error={list.error} />
  const norm = normaliseDomain(input)
  return (
    <section className="card stack" aria-label="Internal email domains">
      <h2>Internal email domains</h2>
      <Banner tone="info">Domain is a heuristic, not proof of employment. Exact-match semantics.</Banner>
      <div className="row">
        {domains.map((d) => (
          <Chip key={d.domain} tone="info">
            {d.domain}
            <button aria-label={`Remove ${d.domain}`} onClick={() => setDraft(domains.filter((x) => x.domain !== d.domain))}>
              ✕
            </button>
          </Chip>
        ))}
      </div>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <label className="field">
          Add domain
          <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="@example.com" />
        </label>
        <button
          className="btn"
          disabled={!norm || domains.some((d) => d.domain === norm)}
          onClick={() => {
            if (norm) setDraft([...domains, { domain: norm }])
            setInput('')
          }}
        >
          Add
        </button>
        {input && <span className="small muted">{norm ? `Will be saved as ${norm}` : 'Not a valid domain'}</span>}
      </div>
      {preview.data && (
        <p className="small" data-testid="classification-preview">
          Classification impact: Internal {preview.data.internal} · External {preview.data.external} · Unknown {preview.data.unknown}
        </p>
      )}
      <div className="modal-actions">
        <button className="btn" disabled={!draft} onClick={() => setDraft(null)}>
          Discard
        </button>
        <button className="btn btn-primary" disabled={!draft} onClick={() => draft && save.mutate(draft)}>
          Save and re-evaluate
        </button>
      </div>
    </section>
  )
}
