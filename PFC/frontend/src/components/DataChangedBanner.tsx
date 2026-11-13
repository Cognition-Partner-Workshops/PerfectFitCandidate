import { Link } from 'react-router-dom'
import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { CurrentResults, MatchRun } from '../api/types'
import { formatDateTime } from '../lib/format'
import { Banner, ErrorState } from './ui'

const active = (status: MatchRun['status']) => status === 'queued' || status === 'running'

/** Stale-run notice: inputs changed after the run finished, so its scores are a point-in-time snapshot. */
export function DataChangedBanner({ run, summary }: { run: MatchRun; summary?: CurrentResults }) {
  const qc = useQueryClient()
  const rerun = useMutation({
    mutationFn: () => api.startMatchRun({ incremental: true, scope: summary || run.scope === 'demands' ? 'demands' : 'full', demandIds: summary ? summary.staleDemandIds : run.scope === 'demands' ? run.demandIds : [] }),
  })
  // Follow the new run until it finishes, then refresh every view of "the latest run" (Match Results, Dashboard summary).
  const startedId = rerun.data?.runId
  const progress = useQuery({
    queryKey: ['run', startedId, 'progress'],
    queryFn: () => api.getMatchRun(startedId ?? 0),
    enabled: startedId !== undefined,
    refetchInterval: (q) => (q.state.data && !active(q.state.data.status) ? false : 1000),
  })
  const finished = progress.data && !active(progress.data.status) ? progress.data.status : undefined
  useEffect(() => {
    if (!startedId) return
    void qc.invalidateQueries({ queryKey: ['runs'] })
    if (!finished) return
    void qc.invalidateQueries({ queryKey: ['run'] })
    void qc.invalidateQueries({ queryKey: ['analytics'] })
    void qc.invalidateQueries({ queryKey: ['current-results'] })
  }, [qc, startedId, finished])
  if (!(summary ?? run).dataChangedSinceRun) return null
  const changedReasons = (summary ?? run).dataChangedReasons
  const reasons = changedReasons.length ? changedReasons.join(', ') : 'source data was updated'
  return (
    <Banner tone="info">
      <strong>Data changed since this run</strong> ({reasons}). These scores are a point-in-time snapshot from {formatDateTime(run.finishedAt)}.{' '}
      <button className="btn btn-sm" disabled={rerun.isPending || rerun.isSuccess} onClick={() => rerun.mutate()}>
        Re-run changed only
      </button>{' '}
      <Link className="btn btn-sm btn-tertiary" to="/demands">
        Full re-run
      </Link>
      {rerun.isSuccess && <span className="small"> Run {rerun.data.runId} started.</span>}
      {rerun.isError && <ErrorState error={rerun.error} />}
    </Banner>
  )
}
