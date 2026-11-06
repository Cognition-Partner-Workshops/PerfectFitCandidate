import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { AdminSettings } from '../../api/types'
import { Banner, ErrorState, Spinner } from '../../components/ui'

type NumKey = { [K in keyof AdminSettings]: AdminSettings[K] extends number ? K : never }[keyof AdminSettings]

const NUMBERS: [NumKey, string, number, number][] = [
  ['analystMinPasswordLength', 'Analyst minimum password length', 10, 64],
  ['adminMinPasswordLength', 'Admin minimum password length', 14, 64],
  ['lockoutAttempts', 'Failed sign-ins before lockout', 3, 10],
  ['lockoutMinutes', 'Lockout duration (minutes)', 5, 120],
  ['idleTimeoutMinutes', 'Idle session timeout (minutes)', 5, 480],
  ['emailsPreselected', 'Emails pre-selected per requisition', 1, 5],
  ['staleDataDays', 'Stale upload warning (days)', 1, 90],
  ['emailTurnaroundHours', 'Email turnaround target (hours)', 1, 168],
]

/** Admin settings: security policy, masking, shortlist/email defaults (GUI_OCC_DESIGN.md §16.14, TDD §9.2, REQ-OUT-03, REQ-MAT-03). */
export function OccAdminSettings() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['occ', 'admin-settings'], queryFn: () => api.getAdminSettings() })
  const [draft, setDraft] = useState<AdminSettings | null>(null)
  const save = useMutation({
    mutationFn: (s: AdminSettings) => api.saveAdminSettings(s),
    onSuccess: (s) => {
      qc.setQueryData(['occ', 'admin-settings'], s)
      setDraft(null)
    },
  })
  if (q.isLoading) return <Spinner />
  if (q.isError || !q.data) return <ErrorState error={q.error} />
  const s = draft ?? q.data
  const invalid = NUMBERS.filter(([k, , min, max]) => !Number.isInteger(s[k]) || s[k] < min || s[k] > Math.min(max, k === 'emailsPreselected' ? s.topN : max)).map(([, label]) => label)
  return (
    <section className="card stack" aria-label="Admin settings">
      <h2>Admin settings</h2>
      <div className="grid-2" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 12 }}>
        {NUMBERS.map(([k, label, min, max]) => (
          <label key={k} className="field">
            {label}
            <input type="number" min={min} max={max} value={s[k]} onChange={(e) => setDraft({ ...s, [k]: Number(e.target.value) })} />
          </label>
        ))}
        <label className="field">
          Shortlist size per requisition
          <input type="number" value={s.topN} readOnly disabled />
        </label>
        <label className="field">
          Email masking (keep / mask)
          <input value={`${s.maskKeep} / ${s.maskCount}`} readOnly disabled />
        </label>
      </div>
      <label className="row" style={{ gap: 6 }}>
        <input type="checkbox" checked={s.analystsMayUnmask} onChange={(e) => setDraft({ ...s, analystsMayUnmask: e.target.checked })} />
        Analysts may unmask emails and phone numbers (every unmask is audit-logged)
      </label>
      <label className="row" style={{ gap: 6 }}>
        <input type="checkbox" checked={s.allowExternalProviders} disabled readOnly />
        Allow external LLM providers (Phase 3)
      </label>
      {invalid.length > 0 && <Banner tone="danger">Out of range: {invalid.join(', ')}</Banner>}
      {save.isError && <Banner tone="danger">{(save.error as Error).message}</Banner>}
      {save.isSuccess && <Banner tone="success">Admin settings saved.</Banner>}
      <div className="row">
        <button className="btn btn-primary" disabled={!draft || invalid.length > 0} onClick={() => draft && save.mutate(draft)}>
          Save admin settings
        </button>
        <button className="btn" disabled={!draft} onClick={() => setDraft(null)}>
          Discard changes
        </button>
      </div>
    </section>
  )
}
