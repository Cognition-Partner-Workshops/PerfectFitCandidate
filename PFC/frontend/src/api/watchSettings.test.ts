import { afterEach, describe, expect, it, vi } from 'vitest'
import { HttpApiClient } from './http'
import { toWatchStatus, type WatchStatusRow } from './mappers'

const source = { enabled: true, path: '/watch', pattern: '*.XLSX', poll_minutes: 12, require_preview: true }
const counts = { rows_read: 3, rows_new: 1, rows_updated: 1, rows_unchanged: 0, rows_rejected: 1, rows_incomplete: 0 }
const run = { run_id: 7, file_name: 'DL.xlsx', status: 'preview' as const, started_at: '2026-01-01T00:00:00Z', committed_at: null, counts }
const status = { enabled: true, reachable: true, last_scan_at: run.started_at, pending: [{ file_name: 'next.xlsx', reason: 'settling' }], last_error: null, last_pickup: run, awaiting_review: [run] }

describe('watch API mapping', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('round-trips enabled source, interval and review preference', async () => {
    const row = { resume_root_path: null, watch: { dl: source, ct: source }, active_model_id: null, density: 'comfortable' }
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(row)))
    vi.stubGlobal('fetch', fetch)
    const api = new HttpApiClient('/api/v1')
    const settings = await api.getMySettings()
    expect(settings.autoSyncDl).toEqual({ enabled: true, watchPath: '/watch', pattern: '*.XLSX', pollMinutes: 12, requirePreview: true })
    await api.saveMySettings(settings)
    expect(JSON.parse(fetch.mock.calls[1][1].body).watch).toEqual({ dl: source, ct: source })
  })

  it('maps counts, pending files, latest pickup and review queue, including empty status', async () => {
    const empty = { ...status, enabled: false, reachable: null, last_scan_at: null, pending: [], last_pickup: null, awaiting_review: [] }
    const row: WatchStatusRow = { dl: status, ct: empty }
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(row)))
    vi.stubGlobal('fetch', fetch)
    const mapped = await new HttpApiClient('/api/v1').getWatchStatus()
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/settings/me/watch-status')
    expect(mapped).toEqual(toWatchStatus(row))
    expect(mapped.dl.pending).toEqual([{ fileName: 'next.xlsx', reason: 'settling' }])
    expect(mapped.dl.awaitingReview[0]).toEqual({ runId: 7, fileName: 'DL.xlsx', status: 'preview', startedAt: run.started_at, committedAt: null, counts: { read: 3, new: 1, updated: 1, unchanged: 0, rejected: 1, incomplete: 0 } })
    expect(mapped.ct.lastPickup).toBeNull()
    expect(mapped.ct.awaitingReview).toEqual([])
  })

  it('loads an existing run and pages all issues', async () => {
    const issue = { row_number: 2, column: 'JD', message: 'Review this row', severity: 'warning' }
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ingestion_run_id: 7, kind: 'DL', file_name: 'DL.xlsx', sheet_name: 'Demands', counts })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: Array.from({ length: 100 }, () => issue), total: 101 })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [issue], total: 101 })))
    vi.stubGlobal('fetch', fetch)
    const preview = await new HttpApiClient('/api/v1').getIngestionPreview(7)
    expect(preview.warnings).toHaveLength(101)
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(['/api/v1/ingestion-runs/7', '/api/v1/ingestion-runs/7/issues?limit=100&offset=0', '/api/v1/ingestion-runs/7/issues?limit=100&offset=100'])
  })
})
