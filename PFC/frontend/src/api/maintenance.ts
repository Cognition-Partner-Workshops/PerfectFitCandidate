export type Entity = 'demand' | 'resource'
export type Collection = 'demands' | 'resources'
export interface LifecycleEvent {
  event_id: number; from_state: string | null; to_state: string; source: string
  outcome: string | null; reason_code: string | null; comment: string | null; created_at: string
}
export interface LifecycleDetail {
  lifecycle: string; updated_at: string; end_source: string | null; end_outcome: string | null
  lifecycle_events: LifecycleEvent[]; code_requisition?: string; demand_request_name?: string
  short_name?: string; candidate_id?: number | null
  account_name?: string | null; code_local_grade?: string | null; grade?: string | null
  region?: string | null; location?: string | null; owner?: string | null; bu?: string | null
  email_address?: string | null; skills_raw?: string | null; bench_status?: string | null
  versions?: { version_no: number; created_at: string; jd_text_norm: string }[]
}
export interface ArchiveItem {
  entity_type: Entity; entity_id: number; label: string; reason: string
  included: number; outcome: string | null
}
export interface ArchiveBatch {
  batch_id: number; kind: 'archive' | 'purge'; status: string; preview_hash: string
  preview: { demand: number; resource: number }; items: ArchiveItem[]
  result: { demand?: number; resource?: number; error?: string } | null
}
export interface ArchivedRow {
  demand_id?: number; resource_id?: number; candidate_id?: number | null
  code_requisition?: string; account_name?: string | null; demand_request_name?: string | null
  short_name?: string; email_address?: string | null; grade?: string | null
  code_local_grade?: string | null; region?: string | null; location?: string | null
  archived_at: string; end_outcome: string | null
}
export interface ArchivePage { items: ArchivedRow[]; total: number; page: number; page_size: number }
export interface EndedPage { items: { demand_id?: number; resource_id?: number; code_requisition?: string; short_name?: string }[]; total: number }
export interface CleanupPreview {
  temp: { count: number; bytes: number }; logs: { count: number; bytes: number }
  orphan_chunks: number; orphan_vectors: number; db_bytes: number
  backups: { name: string; bytes: number }[]
}
export type CleanupOptions = Record<'purge_temp' | 'purge_logs' | 'purge_orphan_vectors' | 'vacuum' | 'backup', boolean>
export type MaintenanceSettings = Record<string, unknown>
type Request = <T>(method: 'GET' | 'POST' | 'PUT', path: string, body?: unknown, raw?: boolean) => Promise<T>

export class MaintenanceApi {
  private request: Request
  constructor(request: Request) { this.request = request }
  reasons = () => this.request<Record<string, string[]>>('GET', '/lifecycle/reasons')
  detail = (entity: Entity, id: number) => entity === 'demand'
    ? this.request<LifecycleDetail>('GET', `/demands/${id}`)
    : this.request<LifecycleDetail>('GET', `/resources/${id}`)
  change = (entity: Entity, id: number, action: 'end' | 'reopen', body: object) =>
    action === 'end' ? this.request('POST', `/lifecycle/${entity}/${id}/end`, body)
      : this.request('POST', `/lifecycle/${entity}/${id}/reopen`, body)
  ended = (entity: Entity, page: number) => entity === 'demand'
    ? this.request<EndedPage>('GET', `/demands?lifecycle=closed&page_size=25&page=${page}`)
    : this.request<EndedPage>('GET', `/resources?lifecycle=off_bench&page_size=25&page=${page}`)
  preview = (kind: 'archive' | 'purge', ended_before?: string) => kind === 'archive'
    ? this.request<ArchiveBatch>('POST', '/occ/archive/preview', { ended_before: ended_before || null })
    : this.request<ArchiveBatch>('POST', '/occ/archive/purge/preview')
  batch = (id: number) => this.request<ArchiveBatch>('GET', `/occ/archive/batches/${id}`)
  select = (id: number, item: ArchiveItem, included: boolean) => this.request<ArchiveBatch>('PUT', `/occ/archive/batches/${id}/items`, {
    items: [{ entity_type: item.entity_type, entity_id: item.entity_id, included }],
  })
  run = (batch: ArchiveBatch) => batch.kind === 'archive'
    ? this.request<{ job_id: number }>('POST', `/occ/archive/batches/${batch.batch_id}/run`, { preview_hash: batch.preview_hash })
    : this.request<{ job_id: number }>('POST', `/occ/archive/purge/batches/${batch.batch_id}/run`, { preview_hash: batch.preview_hash })
  search = (collection: Collection, filters: Record<string, string | number>) =>
    this.request<ArchivePage>('GET', `/archive/${collection}?${new URLSearchParams(Object.entries(filters).map(([k, v]) => [k, String(v)]))}`)
  export = async (collection: Collection, filters: Record<string, string>) => {
    const response = await this.request<Response>('GET', `/archive/${collection}/export.csv?${new URLSearchParams(filters)}`, undefined, true)
    return response.blob()
  }
  settings = () => this.request<MaintenanceSettings>('GET', '/occ/settings')
  saveSettings = (values: MaintenanceSettings) => this.request<MaintenanceSettings>('PUT', '/occ/settings', values)
  cleanupPreview = () => this.request<CleanupPreview>('GET', '/occ/housekeeping/preview')
  cleanup = (options: CleanupOptions) => this.request<{ job_id: number }>('POST', '/occ/housekeeping', options)
}
