import { useEffect, useId, useState, type ReactNode } from 'react'
import { CircleAlert, Eye, EyeOff, Info, OctagonX, TriangleAlert, X, CircleCheck, CircleHelp } from 'lucide-react'
import type { Band, Eligibility, ScoreComponent } from '../api/types'
import { bandColorVar, bandLabel, bandTone } from '../lib/bands'
import { formatPct } from '../lib/format'
import { api } from '../api'

type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export function Chip({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`chip chip-${tone}`} title={title}>
      {children}
    </span>
  )
}

const BAND_ICON: Record<Band, ReactNode> = {
  Strong: <CircleCheck size={12} aria-hidden />,
  Potential: <Info size={12} aria-hidden />,
  Avg: <TriangleAlert size={12} aria-hidden />,
  Weak: <CircleAlert size={12} aria-hidden />,
  'Insufficient data': <CircleHelp size={12} aria-hidden />,
}

/** Band chip: icon + label + tint — never colour alone (NFR-06). */
export function BandChip({ band }: { band: Band }) {
  return (
    <Chip tone={bandTone(band)}>
      {BAND_ICON[band]}
      {bandLabel(band)}
    </Chip>
  )
}

/** Inline Match % doughnut (GUI_OCC_DESIGN.md §3.6, REQ-OUT-01/10). */
export function MatchDoughnut({ score, band, size = 32 }: { score: number; band: Band; size?: number }) {
  const r = size / 2 - 3
  const c = 2 * Math.PI * r
  const pct = Math.max(0, Math.min(100, Math.round(score)))
  return (
    <span className="doughnut" aria-label={`Match ${pct} percent, ${bandLabel(band)}`} role="img">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--color-bg-subtle)" strokeWidth={4} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={bandColorVar(band)}
          strokeWidth={4}
          strokeDasharray={`${(pct / 100) * c} ${c}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          strokeLinecap="round"
        />
      </svg>
      <span aria-hidden>{formatPct(score)}</span>
    </span>
  )
}

const COMPONENT_LABEL: Record<ScoreComponent['component'], string> = {
  skills: 'Skills, experience and domain',
  grade: 'Grade',
  location: 'Location',
  bench_start: 'Bench Start',
  bench_end: 'Bench End',
  status: 'Bench Status',
}

/** Score breakdown bars (REQ-SCR-04). */
export function ScoreBreakdown({ breakdown }: { breakdown: ScoreComponent[] }) {
  return (
    <div className="breakdown" aria-label="Score breakdown">
      {breakdown.map((b) => (
        <div key={b.component}>
          <div className="row">
            <span>{COMPONENT_LABEL[b.component]}</span>
            <span className="spacer" />
            <span className="num">
              {b.points}/{b.maxPoints}
            </span>
          </div>
          <div className="bar">
            <span style={{ width: `${b.maxPoints ? (b.points / b.maxPoints) * 100 : 0}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}

export function EligibilityBadge({ eligibility, reasons }: { eligibility: Eligibility; reasons: string[] }) {
  const title = reasons.join('; ') || undefined
  switch (eligibility) {
    case 'eligible':
      return <Chip tone="success">✓ Eligible</Chip>
    case 'eligible_caveat':
      return (
        <Chip tone="warning" title={title}>
          ⚠ Eligible — caveat
        </Chip>
      )
    case 'excluded':
      return (
        <Chip tone="danger" title={title}>
          ✕ Excluded
        </Chip>
      )
    default:
      return (
        <Chip tone="neutral" title={title}>
          ? Insufficient data
        </Chip>
      )
  }
}

const BANNER_ICON: Record<'warning' | 'danger' | 'info' | 'success', ReactNode> = {
  warning: <TriangleAlert size={16} aria-hidden />,
  danger: <OctagonX size={16} aria-hidden />,
  info: <Info size={16} aria-hidden />,
  success: <CircleCheck size={16} aria-hidden />,
}

export function Banner({ tone, children }: { tone: 'warning' | 'danger' | 'info' | 'success'; children: ReactNode }) {
  return (
    <div className={`banner banner-${tone}`} role={tone === 'danger' ? 'alert' : 'status'}>
      {BANNER_ICON[tone]}
      <div>{children}</div>
    </div>
  )
}

export function EmptyState({ message, action }: { message: string; action?: ReactNode }) {
  return (
    <div className="empty">
      <svg width="72" height="56" viewBox="0 0 72 56" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="6" y="10" width="60" height="40" rx="6" />
        <path d="M6 22h60M18 32h24M18 40h14" />
      </svg>
      <p>{message}</p>
      {action}
    </div>
  )
}

export function Modal({
  title,
  onClose,
  children,
  actions,
}: {
  title: string
  onClose: () => void
  children: ReactNode
  actions?: ReactNode
}) {
  const id = useId()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="overlay" role="presentation">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={id}>
        <div className="row">
          <h2 id={id}>{title}</h2>
          <span className="spacer" />
          <button className="icon-btn" aria-label="Close" onClick={onClose}>
            <X size={18} />
          </button>
        </div>
        {children}
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  )
}

export function KpiTile({ label, value, hint, accent }: { label: string; value: ReactNode; hint?: ReactNode; accent?: string }) {
  return (
    <div className="kpi" style={accent ? { borderTopColor: accent } : undefined}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}

/**
 * Masked email/phone with audited Show/Hide (REQ-OUT-03, REQ-AUTH-05). The unmasked value comes
 * only from `/pii/unmask` and is kept until Hide or navigation.
 */
export function MaskedValue({
  masked,
  entity,
  id,
  field = 'email',
}: {
  masked: string | null
  entity: 'resource' | 'candidate' | 'occ_user'
  id: number
  field?: 'email' | 'phone'
}) {
  const [shown, setShown] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  if (!masked) return <span className="muted">Not recorded</span>
  const toggle = async () => {
    if (shown) {
      setShown(null)
      return
    }
    setBusy(true)
    try {
      setShown(await api.unmask(entity, id, field))
    } finally {
      setBusy(false)
    }
  }
  return (
    <span className="row" style={{ gap: 4, flexWrap: 'nowrap' }}>
      <span className="mono small">{shown ?? masked}</span>
      <button
        className="icon-btn"
        onClick={toggle}
        disabled={busy}
        aria-label={shown ? `Hide ${field}` : `Show ${field}`}
        title={shown ? 'Hide' : 'Show (recorded in the audit log)'}
      >
        {shown ? <EyeOff size={14} /> : <Eye size={14} />}
      </button>
    </span>
  )
}

export function ChartCard({
  title,
  subtitle,
  children,
  table,
  span = 4,
}: {
  title: string
  subtitle?: string
  children: ReactNode
  table: { columns: string[]; rows: (string | number)[][] }
  span?: 4 | 6 | 8 | 12
}) {
  const [asTable, setAsTable] = useState(false)
  return (
    <section className={`card span-${span}`} aria-label={title}>
      <div className="card-header">
        <div>
          <h3>{title}</h3>
          {subtitle && <div className="sub">{subtitle}</div>}
        </div>
        <span className="spacer" />
        <button className="btn btn-tertiary btn-sm" onClick={() => setAsTable((v) => !v)} aria-pressed={asTable}>
          {asTable ? 'View as chart' : 'View as table'}
        </button>
      </div>
      {table.rows.length === 0 ? (
        <EmptyState message="No data for the current filters" />
      ) : asTable ? (
        <div className="table-wrap scroll">
          <table className="data">
            <thead>
              <tr>
                {table.columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((r, i) => (
                <tr key={i}>
                  {r.map((v, j) => (
                    <td key={j} className={typeof v === 'number' ? 'num' : undefined}>
                      {v}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        children
      )}
    </section>
  )
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="empty" role="status" aria-live="polite">
      {label}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const msg = error instanceof Error ? error.message : 'Something went wrong'
  return (
    <Banner tone="danger">
      {msg}{' '}
      {onRetry && (
        <button className="btn btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </Banner>
  )
}
