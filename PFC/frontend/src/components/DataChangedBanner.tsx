import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { MatchRun } from '../api/types'
import { formatDateTime } from '../lib/format'
import { Banner, ErrorState } from './ui'

/** Stale-run notice: inputs changed after the run finished, so its scores are a point-in-time snapshot. */
export function DataChangedBanner({ run }: { run: MatchRun }) {
  const qc = useQueryClient()
  const rerun = useMutation({
    mutationFn: () => api.startMatchRun({ incremental: true, scope: run.scope === 'demands' ? 'demands' : 'full', demandIds: run.scope === 'demands' ? run.demandIds : [] }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['runs'] })
      void qc.invalidateQueries({ queryKey: ['run'] })
    },
  })
  if (!run.dataChangedSinceRun) return null
  const reasons = run.dataChangedReasons.length ? run.dataChangedReasons.join(', ') : 'source data was updated'
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
