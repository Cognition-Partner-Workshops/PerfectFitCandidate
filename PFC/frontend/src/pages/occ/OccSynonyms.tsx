import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { Synonym } from '../../api/types'
import { Chip, ErrorState, Spinner } from '../../components/ui'
import { normaliseTerm } from '../../lib/occ'

/** Skill-synonym tracker (REQ-MAT-05, GUI_OCC_DESIGN.md §16.5). */
export function OccSynonyms() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['occ', 'synonyms'], queryFn: () => api.listSynonyms() })
  const [tab, setTab] = useState<'Active' | 'Suggested'>('Active')
  const [test, setTest] = useState('')
  const [canonical, setCanonical] = useState('')
  const [variants, setVariants] = useState('')
  const [relation, setRelation] = useState<Synonym['relation']>('Exact synonym')
  const refresh = () => void qc.invalidateQueries({ queryKey: ['occ', 'synonyms'] })
  const create = useMutation({
    mutationFn: () => api.createSynonym({ canonical, variants: variants.split(',').map((v) => v.trim()).filter(Boolean), relation }),
    onSuccess: () => {
      setCanonical('')
      setVariants('')
      refresh()
    },
  })
  const update = useMutation({ mutationFn: (v: { id: number; patch: Partial<Synonym> }) => api.updateSynonym(v.id, v.patch), onSuccess: refresh })
  if (list.isLoading) return <Spinner />
  if (list.isError) return <ErrorState error={list.error} />
  const all = list.data ?? []
  const rows = all.filter((s) => s.status === tab)
  const resolved = normaliseTerm(test, all)
  return (
    <section className="card stack" aria-label="Skill synonyms">
      <h2>Skill-synonym tracker</h2>
      <div className="row">
        <label className="field">
          Test a term
          <input value={test} onChange={(e) => setTest(e.target.value)} placeholder="e.g. BA" />
        </label>
        {test && <span data-testid="normalised">{resolved ? `→ ${resolved}` : 'No canonical term'}</span>}
      </div>
      <div className="tabs" role="tablist">
        {(['Active', 'Suggested'] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {t === 'Suggested' ? 'Suggestions' : t} ({all.filter((s) => s.status === t).length})
          </button>
        ))}
      </div>
      <table className="data">
        <thead>
          <tr>
            <th>Canonical term</th>
            <th>Variants</th>
            <th>Relation</th>
            <th>Usage</th>
            <th>Source</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.synonymId}>
              <td>{s.canonical}</td>
              <td className="row">{s.variants.map((v) => <Chip key={v}>{v}</Chip>)}</td>
              <td>{s.relation}</td>
              <td className="num">{s.usageCount}</td>
              <td>{s.source}</td>
              <td>
                {s.status === 'Suggested' && (
                  <span className="row" style={{ flexWrap: 'nowrap' }}>
                    <button className="btn btn-sm" onClick={() => update.mutate({ id: s.synonymId, patch: { status: 'Active' } })}>
                      Accept
                    </button>
                    <button className="btn btn-sm" onClick={() => update.mutate({ id: s.synonymId, patch: { status: 'Rejected' } })}>
                      Reject
                    </button>
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Add canonical term</h3>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <label className="field">
          Canonical
          <input value={canonical} onChange={(e) => setCanonical(e.target.value)} />
        </label>
        <label className="field">
          Variants (comma separated)
          <input value={variants} onChange={(e) => setVariants(e.target.value)} />
        </label>
        <label className="field">
          Relation
          <select value={relation} onChange={(e) => setRelation(e.target.value as Synonym['relation'])}>
            <option>Exact synonym</option>
            <option>Abbreviation</option>
            <option>Spelling variant</option>
            <option>Related</option>
          </select>
        </label>
        <button className="btn btn-primary" disabled={!canonical.trim() || !variants.trim()} onClick={() => create.mutate()}>
          Add
        </button>
      </div>
      {create.isError && <ErrorState error={create.error} />}
      <span className="small muted">Saving publishes a new synonym-set version and offers rescoring.</span>
    </section>
  )
}
