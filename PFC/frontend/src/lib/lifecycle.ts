export const reasonLabel = (value: string) => value.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase())

export const countLabel = (count: number, noun: string) => `${count} ${noun}${count === 1 ? '' : 's'}`

const ARCHIVE_REASONS: Record<string, string> = {
  filled_manual: 'Filled — manual', cancelled_manual: 'Cancelled — manual', closed_manual: 'Closed — manual',
  off_bench_manual: 'Off bench — manual', released_manual: 'Released — manual',
  not_in_latest_dl: 'Not in latest DL', not_in_latest_ct: 'Off bench — not in latest CT',
  selected: 'Selected', not_on_bench: 'Not on Bench', bench_end_set: 'Bench end date set',
  closed_occ: 'Closed — OCC', off_bench_occ: 'Off bench — OCC',
  closed_legacy: 'Closed — legacy record', off_bench_legacy: 'Off bench — legacy record',
  closed_upload: 'Closed — upload', off_bench_upload: 'Off bench — upload',
  closed_status: 'Closed — status', off_bench_status: 'Off bench — status',
  retention_expired: 'Retention expired',
}

export const archiveReasonLabel = (reason: string) => ARCHIVE_REASONS[reason] ?? reasonLabel(reason)
