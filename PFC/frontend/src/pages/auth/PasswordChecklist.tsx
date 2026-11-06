import type { PasswordCheck } from '../../lib/password'

export function PasswordChecklist({ checks }: { checks: PasswordCheck[] }) {
  const score = checks.filter((c) => c.ok).length
  const label = score === checks.length ? 'Strong' : score >= checks.length - 2 ? 'Medium' : 'Weak'
  return (
    <div className="stack" style={{ gap: 4 }} aria-live="polite">
      <span className="small">
        Password strength <span className="num">{'▮'.repeat(score)}{'▯'.repeat(checks.length - score)}</span> {label}
      </span>
      <ul className="checklist">
        {checks.map((c) => (
          <li key={c.id} className={c.ok ? 'ok' : 'no'}>
            {c.ok ? '✓' : '✗'} {c.label}
          </li>
        ))}
      </ul>
    </div>
  )
}
