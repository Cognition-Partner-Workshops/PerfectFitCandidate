import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { PersonalSettings, WatchSourceStatus } from '../../api/types'
import { UploadDialog } from '../../components/UploadDialog'
import { ErrorState } from '../../components/ui'
import { formatDateTime } from '../../lib/format'

const time = (value: string | null) => value ? formatDateTime(value) : 'Not yet'

function SourceStatus({ status, onReview }: { status: WatchSourceStatus; onReview: (id: number) => void }) {
  const last = status.lastPickup
  return <div className="stack small" aria-label="Pickup status">
    <span>Last scan: {time(status.lastScanAt)}</span>
    <span>{status.lastError ?? (status.reachable === true ? 'Folder reachable' : status.reachable === false ? 'Folder unreachable' : 'Folder not checked yet')}</span>
    {status.pending.length > 0 && <div>Pending files<ul>{status.pending.map((file) => <li key={file.fileName}>{file.fileName} — {file.reason}</li>)}</ul></div>}
    {last ? <div>
      Last pickup: {last.fileName} — {last.status === 'no_op' ? 'no changes' : last.status} · {time(last.committedAt ?? last.startedAt)}
      <div className="num">Rows read {last.counts.read} · New {last.counts.new} · Updated {last.counts.updated} · Unchanged {last.counts.unchanged} · Rejected {last.counts.rejected} · Incomplete {last.counts.incomplete}</div>
    </div> : <span>No files picked up yet.</span>}
    <h3>Awaiting review</h3>
    {status.awaitingReview.length === 0 ? <span>No files awaiting review.</span> : <ul>{status.awaitingReview.map((run) => <li key={run.runId}>
      {run.fileName} · {time(run.startedAt)}{' '}<button className="btn" onClick={() => onReview(run.runId)}>Review</button>
    </li>)}</ul>}
  </div>
}

export function WatchSources({ form, onChange }: { form: PersonalSettings; onChange: (patch: Partial<PersonalSettings>) => void }) {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: ['watch-status'], queryFn: () => api.getWatchStatus(), refetchInterval: 15_000 })
  const [review, setReview] = useState<{ kind: 'dl' | 'ct'; id: number } | null>(null)
  return <>
    <p className="small muted">Pickup runs the same validation as manual upload. Files are picked only once fully synced and unchanged; identical files are skipped. With hold-for-review off, a file with error rows still waits for review.</p>
    {status.isError && <ErrorState error={status.error} onRetry={() => { void status.refetch() }} />}
    {(['dl', 'ct'] as const).map((kind) => {
      const key = kind === 'dl' ? 'autoSyncDl' : 'autoSyncCt'
      const source = form[key]
      const set = (patch: Partial<typeof source>) => onChange({ [key]: { ...source, ...patch } })
      return <fieldset key={kind} className="stack" style={{ border: '1px solid var(--color-border)', borderRadius: 8 }}>
        <legend>Auto-sync — {kind === 'dl' ? 'Demand List' : 'Candidate Tracker'}</legend>
        <label className="row small"><input type="checkbox" checked={source.enabled} onChange={(e) => set({ enabled: e.target.checked })} />Enabled</label>
        <label className="field">Watch path<input value={source.watchPath} onChange={(e) => set({ watchPath: e.target.value })} /></label>
        <label className="field">File pattern<input value={source.pattern} onChange={(e) => set({ pattern: e.target.value })} /></label>
        <label className="field">Poll every N minutes<input type="number" min={1} max={1440} step={1} value={Number.isNaN(source.pollMinutes) ? '' : source.pollMinutes} onChange={(e) => set({ pollMinutes: e.target.valueAsNumber })} /></label>
        <label className="row small"><input type="checkbox" checked={source.requirePreview} onChange={(e) => set({ requirePreview: e.target.checked })} />Hold for my review before committing</label>
        {status.isPending && <span className="small muted">Loading pickup status…</span>}
        {status.data && <SourceStatus status={status.data[kind]} onReview={(id) => setReview({ kind, id })} />}
      </fieldset>
    })}
    {review && <UploadDialog key={review.id} kind={review.kind} existingRunId={review.id} onClose={() => { setReview(null); void qc.invalidateQueries({ queryKey: ['watch-status'] }) }} />}
  </>
}
