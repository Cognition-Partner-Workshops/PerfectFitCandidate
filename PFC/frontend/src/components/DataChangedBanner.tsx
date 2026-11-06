import { Link } from 'react-router-dom'
import type { MatchRun } from '../api/types'
import { formatDateTime } from '../lib/format'
import { Banner } from './ui'

/** Stale-run notice: inputs changed after the run finished, so its scores are a point-in-time snapshot. */
export function DataChangedBanner({ run }: { run: MatchRun }) {
  if (!run.dataChangedSinceRun) return null
  const reasons = run.dataChangedReasons.length ? run.dataChangedReasons.join(', ') : 'source data was updated'
  return (
    <Banner tone="info">
      <strong>Data changed since this run</strong> ({reasons}). These scores are a point-in-time snapshot from {formatDateTime(run.finishedAt)}.{' '}
      <Link to="/demands">Re-run matching</Link> with Analyze JD → Confirm &amp; Start in Demand List.
    </Banner>
  )
}
