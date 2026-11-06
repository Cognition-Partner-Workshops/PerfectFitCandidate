import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api'
import { formatDateTime } from '../lib/format'

function message(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) return 'The local AI is unavailable. Try again when it is ready.'
    if (error.status === 409) return 'No source document is recorded. Add a CV or job description first.'
    if (error.status === 422) return 'The AI did not return a valid, source-supported summary. Try again.'
  }
  return 'The summary could not be loaded or generated. Please try again.'
}

export function SummaryCard({ kind, id }: { kind: 'candidate' | 'demand'; id: number }) {
  const client = useQueryClient()
  const key = ['summary', kind, id]
  const saved = useQuery({
    queryKey: key,
    queryFn: () => kind === 'candidate' ? api.getCandidateSummary(id) : api.getDemandSummary(id),
    retry: false,
  })
  const generation = useMutation({
    mutationFn: () => kind === 'candidate' ? api.generateCandidateSummary(id) : api.generateDemandSummary(id),
    onSuccess: (summary) => client.setQueryData(key, summary),
  })
  const summary = saved.data
  const missing = saved.error instanceof ApiError && saved.error.code === 'SUMMARY_NOT_FOUND'
  const error = generation.error ?? (missing ? null : saved.error)
  return (
    <section className="card stack" aria-label="Summary">
      <h2>Summary</h2>
      <p className="small muted">AI summary for reading only — not used in scoring. Check against the source.</p>
      {summary?.stale && <p role="status">Source changed since this summary — regenerate</p>}
      {summary?.sentences.map((sentence, index) => (
        <div key={index}>
          <p>{sentence.text}</p>
          <details><summary>Evidence</summary><blockquote>{sentence.evidence}</blockquote></details>
        </div>
      ))}
      {summary && <p className="small muted">{summary.model} · {formatDateTime(summary.createdAt)}</p>}
      {error && <p role="alert">{message(error)}</p>}
      {saved.isLoading && <p role="status">Loading summary…</p>}
      <button className="btn" disabled={saved.isLoading || generation.isPending} onClick={() => generation.mutate()}>
        {generation.isPending ? 'Generating…' : summary ? 'Regenerate' : 'Generate summary'}
      </button>
    </section>
  )
}
