import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { SkillSuggestion, SkillSuggestionAcceptItem, SkillSuggestionRelation, Synonym } from '../../api/types'
import { Chip, ErrorState, Spinner } from '../../components/ui'
import { normaliseTerm } from '../../lib/occ'

type Tab = 'Active' | 'Suggestions' | 'Rejected'
type Mapping = { canonical: string; relation: SkillSuggestionRelation }

const RELATIONS: { value: SkillSuggestionRelation; label: string }[] = [
  { value: 'equivalent', label: 'Equivalent' },
  { value: 'abbreviation', label: 'Abbreviation' },
  { value: 'misspelling', label: 'Misspelling' },
  { value: 'related', label: 'Related' },
]

/** Skill-synonym tracker (REQ-MAT-05, GUI_OCC_DESIGN.md §16.5). */
export function OccSynonyms() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['occ', 'synonyms'], queryFn: () => api.listSynonyms() })
  const suggestions = useQuery({ queryKey: ['occ', 'skill-suggestions'], queryFn: () => api.listSkillSuggestions() })
  const rejected = useQuery({ queryKey: ['occ', 'skill-suggestions', 'rejected'], queryFn: () => api.listRejectedSkillTerms() })
  const [tab, setTab] = useState<Tab>('Active')
  const [test, setTest] = useState('')
  const [canonical, setCanonical] = useState('')
  const [variants, setVariants] = useState('')
  const [relation, setRelation] = useState<Synonym['relation']>('Exact synonym')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [mapping, setMapping] = useState<Record<string, Mapping>>({})
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['occ', 'synonyms'] })
    void qc.invalidateQueries({ queryKey: ['occ', 'skill-suggestions'] })
    void qc.invalidateQueries({ queryKey: ['resources'] })
  }
  const create = useMutation({
    mutationFn: () => api.createSynonym({ canonical, variants: variants.split(',').map((v) => v.trim()).filter(Boolean), relation }),
    onSuccess: () => {
      setCanonical('')
      setVariants('')
      refresh()
    },
  })
  const update = useMutation({ mutationFn: (v: { id: number; patch: Partial<Synonym> }) => api.updateSynonym(v.id, v.patch), onSuccess: refresh })
  const accept = useMutation({ mutationFn: (items: SkillSuggestionAcceptItem[]) => api.acceptSkillSuggestions(items), onSuccess: () => { setSelected(new Set()); refresh() } })
  const reject = useMutation({ mutationFn: (terms: string[]) => api.rejectSkillSuggestions(terms), onSuccess: () => { setSelected(new Set()); refresh() } })
  const restore = useMutation({ mutationFn: (termNorms: string[]) => api.restoreRejectedSkillTerms(termNorms), onSuccess: refresh })
  if (list.isLoading) return <Spinner />
  if (list.isError) return <ErrorState error={list.error} />

  const all = list.data ?? []
  const legacyRows = all.filter((s) => s.status === 'Suggested')
  const activeRows = all.filter((s) => s.status === 'Active')
  const resolved = normaliseTerm(test, all)
  const visible = (suggestions.data ?? []).filter((s) =>
    `${s.term} ${s.closest ?? ''}`.toLowerCase().includes(search.toLowerCase()),
  )
  const visibleNorms = visible.map((s) => s.termNorm)
  const allVisibleSelected = visibleNorms.length > 0 && visibleNorms.every((norm) => selected.has(norm))
  const canonicalOptions = [...new Set(activeRows.map((s) => s.canonical))]
  const acceptAsNew = (term: string) => accept.mutate([{ term }])
  const mapToExisting = (suggestion: SkillSuggestion) => {
    const selectedMapping = mapping[suggestion.termNorm]
    if (!selectedMapping?.canonical) return
    accept.mutate([{
      term: suggestion.term,
      canonical: selectedMapping.canonical,
      relation: selectedMapping.relation,
      credit: selectedMapping.relation === 'related' ? 0.6 : 1,
    }])
  }
  const toggleOne = (norm: string) => setSelected((current) => {
    const next = new Set(current)
    if (next.has(norm)) next.delete(norm)
    else next.add(norm)
    return next
  })
  const toggleVisible = () => setSelected((current) => {
    const next = new Set(current)
    if (allVisibleSelected) visibleNorms.forEach((norm) => next.delete(norm))
    else visibleNorms.forEach((norm) => next.add(norm))
    return next
  })
  const selectedRows = visible.filter((s) => selected.has(s.termNorm))
  const foundIn = (s: SkillSuggestion) => [
    s.ctPeople > 0 ? `CT: ${s.ctPeople} people` : '',
    s.cvPeople > 0 ? `CV: ${s.cvPeople} people` : '',
    s.jdDemands > 0 ? `JD: ${s.jdDemands} demands` : '',
  ].filter(Boolean).join(' · ') || '—'

  return (
    <section className="card stack" aria-label="Skill synonyms">
      <h2>Skill-synonym tracker</h2>
      <div className="row">
        <label className="field">Test a term<input value={test} onChange={(e) => setTest(e.target.value)} placeholder="e.g. BA" /></label>
        {test && <span data-testid="normalised">{resolved ? `→ ${resolved}` : 'No canonical term'}</span>}
      </div>
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === 'Active'} onClick={() => setTab('Active')}>Active ({activeRows.length})</button>
        <button role="tab" aria-selected={tab === 'Suggestions'} onClick={() => setTab('Suggestions')}>Suggestions ({suggestions.data?.length ?? 0})</button>
        <button role="tab" aria-selected={tab === 'Rejected'} onClick={() => setTab('Rejected')}>Rejected ({rejected.data?.length ?? 0})</button>
      </div>
      {tab === 'Active' && (
        <>
          <table className="data">
            <thead><tr><th>Canonical term</th><th>Variants</th><th>Relation</th><th>Usage</th><th>Source</th><th /></tr></thead>
            <tbody>{activeRows.map((s) => <tr key={s.synonymId}>
              <td>{s.canonical}</td><td className="row">{s.variants.map((v) => <Chip key={v}>{v}</Chip>)}</td>
              <td>{s.relation}</td><td className="num">{s.usageCount}</td><td>{s.source}</td><td />
            </tr>)}</tbody>
          </table>
          <h3>Add canonical term</h3>
          <div className="row" style={{ alignItems: 'flex-end' }}>
            <label className="field">Canonical<input value={canonical} onChange={(e) => setCanonical(e.target.value)} /></label>
            <label className="field">Variants (comma separated)<input value={variants} onChange={(e) => setVariants(e.target.value)} /></label>
            <label className="field">Relation<select value={relation} onChange={(e) => setRelation(e.target.value as Synonym['relation'])}>
              <option>Exact synonym</option><option>Abbreviation</option><option>Spelling variant</option><option>Related</option>
            </select></label>
            <button className="btn btn-primary" disabled={!canonical.trim() || !variants.trim()} onClick={() => create.mutate()}>Add</button>
          </div>
          {create.isError && <ErrorState error={create.error} />}
          <span className="small muted">Saving publishes a new synonym-set version and offers rescoring.</span>
        </>
      )}
      {tab === 'Suggestions' && (
        <div className="stack">
          <label className="field">Search suggestions
            <input aria-label="Search suggestions" value={search} onChange={(e) => { setSearch(e.target.value); setSelected(new Set()) }} placeholder="Search terms or closest skill" />
          </label>
          {suggestions.isLoading ? <Spinner /> : suggestions.isError ? <ErrorState error={suggestions.error} /> : (
            <>
              <div className="row">
                <label className="row"><input aria-label="Select all visible suggestions" type="checkbox" checked={allVisibleSelected} onChange={toggleVisible} /> Select all</label>
                <button className="btn btn-sm" disabled={!selectedRows.length || accept.isPending} onClick={() => accept.mutate(selectedRows.map((s) => ({ term: s.term })))}>Add selected as new skills ({selectedRows.length})</button>
                <button className="btn btn-sm" disabled={!selectedRows.length || reject.isPending} onClick={() => reject.mutate(selectedRows.map((s) => s.term))}>Reject selected ({selectedRows.length})</button>
              </div>
              <table className="data">
                <thead><tr><th>Select</th><th>Term</th><th>Found in</th><th>Closest skill</th><th>Actions</th></tr></thead>
                <tbody>{visible.map((s) => {
                  const current = mapping[s.termNorm] ?? { canonical: '', relation: 'equivalent' as const }
                  return <tr key={s.termNorm}>
                    <td><input type="checkbox" aria-label={`Select ${s.term}`} checked={selected.has(s.termNorm)} onChange={() => toggleOne(s.termNorm)} /></td>
                    <td>{s.term}</td><td>{foundIn(s)}</td><td>{s.closest ?? '—'}</td>
                    <td><div className="row">
                      <button className="btn btn-sm" onClick={() => acceptAsNew(s.term)}>Add as new skill</button>
                      <select aria-label={`Canonical for ${s.term}`} value={current.canonical} onChange={(e) => setMapping((old) => ({ ...old, [s.termNorm]: { ...current, canonical: e.target.value } }))}>
                        <option value="">Map to…</option>{canonicalOptions.map((name) => <option key={name} value={name}>{name}</option>)}
                      </select>
                      <select aria-label={`Relation for ${s.term}`} value={current.relation} onChange={(e) => setMapping((old) => ({ ...old, [s.termNorm]: { ...current, relation: e.target.value as SkillSuggestionRelation } }))}>
                        {RELATIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                      </select>
                      <button className="btn btn-sm" disabled={!current.canonical} onClick={() => mapToExisting(s)}>Map to existing</button>
                      <button className="btn btn-sm" onClick={() => reject.mutate([s.term])}>Reject</button>
                    </div></td>
                  </tr>
                })}</tbody>
              </table>
              {legacyRows.length > 0 && <section className="card stack" aria-label="Legacy synonym suggestions">
                <h3>Legacy suggestions</h3>
                {legacyRows.map((s) => <div className="row" key={s.synonymId}>
                  <span>{s.variants.join(', ') || s.canonical} → {s.canonical}</span>
                  <button className="btn btn-sm" onClick={() => update.mutate({ id: s.synonymId, patch: { status: 'Active' } })}>Accept</button>
                  <button className="btn btn-sm" onClick={() => update.mutate({ id: s.synonymId, patch: { status: 'Rejected' } })}>Reject</button>
                </div>)}
              </section>}
            </>
          )}
          {accept.isError && <ErrorState error={accept.error} />}
          {reject.isError && <ErrorState error={reject.error} />}
        </div>
      )}
      {tab === 'Rejected' && (
        <div className="stack">
          {rejected.isLoading ? <Spinner /> : rejected.isError ? <ErrorState error={rejected.error} /> : (
            <table className="data">
              <thead><tr><th>Term</th><th>Rejected at</th><th /></tr></thead>
              <tbody>{(rejected.data ?? []).map((s) => <tr key={s.termNorm}>
                <td>{s.term}</td><td>{s.rejectedAt}</td><td><button className="btn btn-sm" onClick={() => restore.mutate([s.termNorm])}>Restore</button></td>
              </tr>)}</tbody>
            </table>
          )}
          {restore.isError && <ErrorState error={restore.error} />}
        </div>
      )}
    </section>
  )
}
