import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import { Banner, EmptyState, ErrorState, Modal, Spinner } from '../../components/ui'
import { formatDateTime } from '../../lib/format'

export function OccCvExtractions() {
  const qc = useQueryClient()
  const [candidateId, setCandidateId] = useState<number | null>(null)
  const [scope, setScope] = useState<number | 'all' | null>(null)
  const candidates = useQuery({ queryKey: ['resources', 'all'], queryFn: () => api.listAllResources() })
  const list = useQuery({ queryKey: ['occ', 'cv-extractions', candidateId], queryFn: () => api.listCvExtractions(candidateId!), enabled: candidateId !== null })
  const clear = useMutation({
    mutationFn: (target: number | 'all') => target === 'all' ? api.clearAllExtractions() : api.clearCandidateExtractions(target),
    onSuccess: async () => {
      setScope(null)
      await qc.invalidateQueries()
    },
  })
  const rows = [...new Map((candidates.data ?? []).filter((r) => r.candidateId).map((r) => [r.candidateId, r])).values()]
  return (
    <section className="card stack" aria-label="Cached CV text">
      <h2>Clear cached CV text</h2>
      <p>Clear extracted text and facts, not the original CV files.</p>
      {candidates.isLoading && <Spinner />}
      {candidates.isError && <ErrorState error={candidates.error} />}
      <label className="field">Candidate
        <select value={candidateId ?? ''} disabled={clear.isPending} onChange={(e) => { setCandidateId(e.target.value ? Number(e.target.value) : null); clear.reset() }}>
          <option value="">Select a candidate</option>
          {rows.map((r) => <option key={r.candidateId} value={r.candidateId!}>{r.name ?? r.shortName} · {r.shortName} · #{r.candidateId}</option>)}
        </select>
      </label>
      <label className="field">Candidate ID (including candidates without a CT record)
        <input type="number" min="1" step="1" value={candidateId ?? ''} disabled={clear.isPending} onChange={(e) => {
          const id = Number(e.target.value)
          setCandidateId(Number.isInteger(id) && id > 0 ? id : null)
          clear.reset()
        }} />
      </label>
      {list.isLoading && <Spinner />}
      {list.isError && <ErrorState error={list.error} />}
      {list.data && (list.data.length === 0 ? <EmptyState message="No cached CV versions for this candidate." /> :
        <table className="data" aria-label="Cached CV extractions">
          <thead><tr><th>File (masked)</th><th>Version</th><th>Extracted at</th><th>Removed</th></tr></thead>
          <tbody>{list.data.map((v) => <tr key={v.cvVersionId}><td>{v.fileName}</td><td>{v.versionNo}</td><td>{formatDateTime(v.extractedAt)}</td><td>{v.removed ? 'Yes' : 'No'}</td></tr>)}</tbody>
        </table>)}
      {clear.data && <Banner tone="success">
        Versions cleared: {clear.data.versionsCleared} · Chunks deleted: {clear.data.chunksDeleted} · Vectors deleted: {clear.data.vectorsDeleted} · Matching vectors deleted: {clear.data.matchingVectorsDeleted}
      </Banner>}
      {clear.isError && scope === null && <ErrorState error={clear.error} />}
      <div className="row">
        <button className="btn" disabled={candidateId === null || !list.data || clear.isPending} onClick={() => { clear.reset(); setScope(candidateId) }}>Clear this candidate's extractions</button>
        <button className="btn" disabled={clear.isPending} onClick={() => { clear.reset(); setScope('all') }}>Clear all candidates' extractions</button>
      </div>
      {scope !== null && <Modal title={scope === 'all' ? 'Clear all cached CV text?' : `Clear cached CV text for candidate #${scope}?`} onClose={() => { if (!clear.isPending) setScope(null) }} actions={<>
        <button className="btn" disabled={clear.isPending} onClick={() => setScope(null)}>Cancel</button>
        <button className="btn btn-primary" disabled={clear.isPending} onClick={() => clear.mutate(scope)}>Confirm clear</button>
      </>}>
        <p>This clears {scope === 'all' ? 'all candidates’' : 'this candidate’s'} cached CV text, facts and vectors. Original CV files, candidate identities and past match runs are kept. The next CV sync re-reads the files.</p>
        {clear.isError && <ErrorState error={clear.error} />}
      </Modal>}
    </section>
  )
}
