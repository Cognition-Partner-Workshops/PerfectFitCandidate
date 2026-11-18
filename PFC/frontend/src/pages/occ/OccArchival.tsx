import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { ArchiveBatch, ArchiveItem, CleanupOptions } from '../../api/maintenance'
import { Banner, ErrorState, Modal } from '../../components/ui'
import { useTasks } from '../../lib/tasks'
import { archiveReasonLabel, countLabel } from '../../lib/lifecycle'

const sizes = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(2)} MB`
const CLEANUP: [keyof CleanupOptions, string][] = [
  ['purge_temp', 'Delete expired temp files'], ['purge_logs', 'Delete expired logs'],
  ['purge_orphan_vectors', 'Remove orphan vectors'], ['vacuum', 'Compact database (VACUUM)'], ['backup', 'Create backup'],
]

function BatchPreview({ batch, onChange }: { batch: ArchiveBatch; onChange: (batch: ArchiveBatch) => void }) {
  const [entity, setEntity] = useState<'demand' | 'resource'>('demand')
  const [confirm, setConfirm] = useState(false)
  const [optimistic, setOptimistic] = useState<{ item: ArchiveItem; checked: boolean } | null>(null)
  const { addJob } = useTasks()
  const qc = useQueryClient()
  const poll = useQuery({ queryKey: ['archive-batch', batch.batch_id], queryFn: () => api.maintenance.batch(batch.batch_id),
    enabled: batch.status === 'running', refetchInterval: (q) => q.state.data?.status === 'running' || !q.state.data ? 1000 : false })
  const current = poll.data ?? batch
  useEffect(() => {
    if (poll.data && batch.status === 'running' && poll.data.status !== 'running') {
      onChange(poll.data)
      void qc.invalidateQueries({ predicate: (query) => query.queryKey[0] !== 'archive-batch' })
    }
  }, [poll.data, batch.status, onChange, qc])
  const change = useMutation({ mutationFn: ({ item, checked }: { item: ArchiveItem; checked: boolean }) => api.maintenance.select(current.batch_id, item, checked), onSuccess: onChange, onSettled: () => setOptimistic(null) })
  const run = useMutation({ mutationFn: () => api.maintenance.run(current), onSuccess: ({ job_id }) => {
    addJob(job_id, current.kind === 'archive' ? 'Archive records' : 'Retention purge')
    onChange({ ...current, status: 'running' }); setConfirm(false); void qc.invalidateQueries({ queryKey: ['archive'] })
  } })
  const selected = current.items.filter((item) => item.included)
  const demands = selected.filter((item) => item.entity_type === 'demand').length
  const resources = selected.length - demands
  const verb = current.kind === 'archive' ? 'Archive' : 'Purge'
  return <section className="stack" aria-label={`${verb} preview`}>
    <p><strong>{selected.length} selected</strong> · {countLabel(demands, 'demand')} · {countLabel(resources, 'resource')} · Status: {current.status}</p>
    <div className="tabs" role="tablist" aria-label={`${verb} entity types`}>{(['demand', 'resource'] as const).map((value) =>
      <button key={value} role="tab" aria-selected={entity === value} onClick={() => setEntity(value)}>{value === 'demand' ? 'Demands' : 'Resources'} ({current.items.filter((i) => i.entity_type === value).length})</button>)}</div>
    <div className="table-wrap"><table className="data" aria-label={`${verb} preview items`}><thead><tr><th>Include</th><th>Record</th><th>Reason</th><th>Outcome</th></tr></thead><tbody>
      {current.items.filter((item) => item.entity_type === entity).map((item) => <tr key={item.entity_id}>
        <td><input type="checkbox" aria-label={`Include ${item.label}`} checked={optimistic?.item.entity_type === item.entity_type && optimistic.item.entity_id === item.entity_id ? optimistic.checked : Boolean(item.included)} disabled={current.status !== 'previewed' || change.isPending} onChange={(e) => { const selection = { item, checked: e.target.checked }; setOptimistic(selection); change.mutate(selection) }} /></td>
        <td>{item.label}</td><td>{archiveReasonLabel(item.reason)}</td><td>{item.outcome ?? '—'}</td>
      </tr>)}
    </tbody></table></div>
    {current.items.length === 0 && <p>No eligible records found.</p>}
    {current.result?.error && <Banner tone="danger">{current.result.error}</Banner>}
    {current.status === 'completed' && <Banner tone="success">{verb} completed. {countLabel(current.result?.demand ?? 0, 'demand')} and {countLabel(current.result?.resource ?? 0, 'resource')} processed.</Banner>}
    {(change.isError || run.isError || poll.isError) && <ErrorState error={change.error ?? run.error ?? poll.error} />}
    <button className={`btn ${verb === 'Purge' ? 'btn-danger' : 'btn-primary'}`} disabled={!selected.length || current.status !== 'previewed' || change.isPending || run.isPending} onClick={() => setConfirm(true)}>{verb} {countLabel(selected.length, 'record')}</button>
    {confirm && <Modal title={`Confirm ${verb.toLowerCase()}`} onClose={() => setConfirm(false)} actions={<>
      <button className="btn" onClick={() => setConfirm(false)}>Cancel</button><button className="btn btn-primary" disabled={run.isPending} onClick={() => run.mutate()}>Confirm {verb.toLowerCase()}</button>
    </>}><p>{verb} exactly {countLabel(demands, 'demand')} and {countLabel(resources, 'resource')} from this preview?</p><p>{verb === 'Purge' ? 'This permanently deletes the records and their dependent data. Audit and redacted lifecycle history are retained.' : 'These records leave active views. Their history remains searchable in Archive.'}</p>{run.isError && <ErrorState error={run.error} />}</Modal>}
  </section>
}

export function OccArchival() {
  const [batch, setBatch] = useState<ArchiveBatch | null>(null)
  const [endedBefore, setEndedBefore] = useState('')
  const [retention, setRetention] = useState<number | null>(null)
  const [cleanup, setCleanup] = useState<CleanupOptions>({ purge_temp: false, purge_logs: false, purge_orphan_vectors: false, vacuum: false, backup: false })
  const [confirmCleanup, setConfirmCleanup] = useState(false)
  const qc = useQueryClient()
  const { addJob } = useTasks()
  const settings = useQuery({ queryKey: ['maintenance-settings'], queryFn: () => api.maintenance.settings() })
  const house = useQuery({ queryKey: ['housekeeping'], queryFn: () => api.maintenance.cleanupPreview() })
  const preview = useMutation({ mutationFn: (kind: 'archive' | 'purge') => api.maintenance.preview(kind, kind === 'archive' ? endedBefore : undefined), onSuccess: setBatch })
  const save = useMutation({ mutationFn: () => api.maintenance.saveSettings({ 'archive.retention_months': retention }), onSuccess: () => { setRetention(null); void qc.invalidateQueries({ queryKey: ['maintenance-settings'] }); setBatch(null) } })
  const runCleanup = useMutation({ mutationFn: () => api.maintenance.cleanup(cleanup), onSuccess: ({ job_id }) => { addJob(job_id, 'Housekeeping'); setConfirmCleanup(false) } })
  const months = retention ?? Number(settings.data?.['archive.retention_months'] ?? 12)
  const busy = batch?.status === 'running'
  return <div className="stack">
    <section className="card stack" aria-label="Archival & housekeeping"><h2>Archival &amp; housekeeping</h2>
      <p>Preview ended records, choose which to archive, then confirm. Building a new preview expires the previous one.</p>
      <label className="field">Ended before (optional)<input type="date" value={endedBefore} onChange={(e) => setEndedBefore(e.target.value)} /></label>
      <button className="btn btn-primary" disabled={preview.isPending || busy} onClick={() => preview.mutate('archive')}>Build archive preview</button>
      {preview.isError && <ErrorState error={preview.error} />}
      {batch?.kind === 'archive' && <BatchPreview key={batch.batch_id} batch={batch} onChange={setBatch} />}
    </section>
    <section className="card stack" aria-label="Retention purge"><h2>Retention purge</h2>
      <p>Only records archived more than {countLabel(months, 'calendar month')} ago are eligible for purge. Purge is explicit and irreversible; it never runs automatically.</p>
      <label className="field">Retention (months)<input type="number" min={1} max={120} value={months} onChange={(e) => setRetention(Number(e.target.value))} /></label>
      <button className="btn" disabled={retention === null || !Number.isInteger(months) || months < 1 || months > 120 || save.isPending || settings.isError} onClick={() => save.mutate()}>Save retention</button>
      {(save.isError || settings.isError) && <ErrorState error={save.error ?? settings.error} />}
      <button className="btn" disabled={preview.isPending || busy || retention !== null} onClick={() => preview.mutate('purge')}>Build purge preview</button>
      {batch?.kind === 'purge' && <BatchPreview key={batch.batch_id} batch={batch} onChange={setBatch} />}
    </section>
    <section className="card stack" aria-label="Housekeeping"><h2>Housekeeping</h2>
      <button className="btn" onClick={() => void house.refetch()}>Refresh housekeeping preview</button>
      {house.isError && <ErrorState error={house.error} />}
      {house.data && <>
        <p>Temp files older than {countLabel(Number(settings.data?.['housekeeping.temp_days'] ?? 7), 'day')}: {house.data.temp.count} ({sizes(house.data.temp.bytes)})</p>
        <p>Logs older than {countLabel(Number(settings.data?.['housekeeping.log_days'] ?? 45), 'day')}: {house.data.logs.count} ({sizes(house.data.logs.bytes)}). Active logs are never removed.</p>
        <p>Orphan chunks: {house.data.orphan_chunks} · Orphan vectors: {house.data.orphan_vectors} · Database: {sizes(house.data.db_bytes)}</p>
        <h3>Housekeeping backups</h3><p>Keep the newest {countLabel(Number(settings.data?.['housekeeping.backups_keep'] ?? 3), 'housekeeping backup')}. Other backups are never rotated.</p>
        {house.data.backups.length ? <ul>{house.data.backups.map((backup) => <li key={backup.name}>{backup.name} · {sizes(backup.bytes)}</li>)}</ul> : <p className="muted">No housekeeping backups yet.</p>}
      </>}
      {CLEANUP.map(([key, label]) => <label className="row" key={key}><input type="checkbox" checked={cleanup[key]} onChange={(e) => setCleanup({ ...cleanup, [key]: e.target.checked })} />{label}</label>)}
      <button className="btn btn-primary" disabled={!Object.values(cleanup).some(Boolean) || runCleanup.isPending || !house.data} onClick={() => setConfirmCleanup(true)}>Run housekeeping</button>
      {runCleanup.isError && <ErrorState error={runCleanup.error} />}
      {confirmCleanup && <Modal title="Confirm housekeeping" onClose={() => setConfirmCleanup(false)} actions={<><button className="btn" onClick={() => setConfirmCleanup(false)}>Cancel</button><button className="btn btn-primary" disabled={runCleanup.isPending} onClick={() => runCleanup.mutate()}>Confirm housekeeping</button></>}>
        <p>Run the selected maintenance tasks?</p><ul>{CLEANUP.filter(([key]) => cleanup[key]).map(([key, label]) => <li key={key}>{label}</li>)}</ul>
        {runCleanup.isError && <ErrorState error={runCleanup.error} />}
      </Modal>}
    </section>
  </div>
}
