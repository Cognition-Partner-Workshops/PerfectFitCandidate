const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** Single GUI date format `DD MMM YYYY` (GQ-11, REQ-OUT-02). ISO `YYYY-MM-DD[...]` input. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return 'Not recorded'
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  if (!m) return `Unparseable: ${value}`
  const month = MONTHS[Number(m[2]) - 1]
  if (!month) return `Unparseable: ${value}`
  return `${m[3]} ${month} ${m[1]}`
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return 'Not recorded'
  const t = /T(\d{2}):(\d{2})/.exec(value)
  return t ? `${formatDate(value)} ${t[1]}:${t[2]}` : formatDate(value)
}

/** Match % is always an integer percentage (REQ-OUT-10). */
export function formatPct(score: number): string {
  return `${Math.round(score)} %`
}

export function orNotRecorded(value: string | null | undefined): string {
  return value && value.trim() ? value : 'Not recorded'
}

/** Blank role location renders as "Unspecified" (REQ-OUT-01). */
export function roleLocation(value: string | null | undefined): string {
  return value && value.trim() ? value : 'Unspecified'
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  const first = parts[0]?.[0] ?? ''
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? '') : ''
  return (first + last).toUpperCase()
}
