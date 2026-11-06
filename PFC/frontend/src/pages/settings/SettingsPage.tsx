import { useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { PersonalSettings } from '../../api/types'
import { useAuth } from '../../auth/context'
import { Banner, ErrorState, Spinner } from '../../components/ui'
import { ChangePasswordForm } from '../auth/ChangePasswordForm'
import { SettingsPackagePanel } from './SettingsPackagePanel'
import { EMAIL_PLACEHOLDERS, unknownPlaceholders } from '../../lib/emailTemplate'
import { access } from '../../lib/permissions'

const SECTIONS = [
  ['account', 'Account'],
  ['data', 'Data sources'],
  ['llm', 'LLM provider'],
  ['email', 'Email'],
  ['template', 'Email template'],
  ['package', 'Settings package'],
  ['display', 'Display'],
] as const

function useSettingsForm() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['settings'], queryFn: () => api.getMySettings() })
  const [draft, setForm] = useState<PersonalSettings | null>(null)
  const form = draft ?? q.data ?? null
  const save = useMutation({
    mutationFn: (s: PersonalSettings) => api.saveMySettings(s),
    onSuccess: (s) => {
      qc.setQueryData(['settings'], s)
      setForm(null)
    },
  })
  return { q, form, setForm, save }
}

function Footer({ dirty, onDiscard, onSave, saved }: { dirty: boolean; onDiscard: () => void; onSave: () => void; saved: boolean }) {
  return (
    <div className="modal-actions">
      {saved && !dirty && <span className="small" style={{ color: 'var(--color-success)' }}>Saved</span>}
      <button className="btn" disabled={!dirty} onClick={onDiscard}>
        Discard
      </button>
      <button className="btn btn-primary" disabled={!dirty} onClick={onSave}>
        Save
      </button>
    </div>
  )
}

function FormSection({ section }: { section: 'data' | 'llm' | 'email' | 'template' | 'display' }) {
  const { user } = useAuth()
  const { q, form, setForm, save } = useSettingsForm()
  const models = useQuery({ queryKey: ['models'], queryFn: () => api.listModels(), enabled: section === 'llm' })
  if (q.isLoading || !form) return <Spinner />
  if (q.isError) return <ErrorState error={q.error} />
  const dirty = JSON.stringify(form) !== JSON.stringify(q.data)
  const set = (patch: Partial<PersonalSettings>) => setForm({ ...form, ...patch })
  const footer = <Footer dirty={dirty} saved={save.isSuccess} onDiscard={() => setForm(null)} onSave={() => save.mutate(form)} />
  const unknown = unknownPlaceholders(form.emailTemplate)
  return (
    <section className="card stack">
      {save.isError && <ErrorState error={save.error} />}
      {section === 'data' && (
        <>
          <h2>Data sources</h2>
          <label className="field">
            Resume folder path
            <input value={form.resumeFolderPath} onChange={(e) => set({ resumeFolderPath: e.target.value })} />
          </label>
          {(['autoSyncDl', 'autoSyncCt'] as const).map((k) => (
            <fieldset key={k} className="stack" style={{ border: '1px solid var(--color-border)', borderRadius: 8 }}>
              <legend>Auto-sync — {k === 'autoSyncDl' ? 'Demand List' : 'Candidate Tracker'} (placeholder, Phase 2)</legend>
              <label className="row small">
                <input type="checkbox" checked={false} disabled /> Enabled (disabled in this version)
              </label>
              <label className="field">
                Watch path
                <input value={form[k].watchPath} onChange={(e) => set({ [k]: { ...form[k], watchPath: e.target.value } })} />
              </label>
              <label className="field">
                File pattern
                <input value={form[k].pattern} onChange={(e) => set({ [k]: { ...form[k], pattern: e.target.value } })} />
              </label>
            </fieldset>
          ))}
        </>
      )}
      {section === 'llm' && (
        <>
          <h2>LLM provider</h2>
          <label className="field">
            Active local model {user && access(user.role, 'settingsChooseModel') === 'P' && <span className="small muted">(admin-approved list only)</span>}
            <select value={form.activeModel} onChange={(e) => set({ activeModel: e.target.value })}>
              {(models.data ?? [])
                .filter((m) => m.purpose === 'generation' && m.approved)
                .map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name} · ~{m.ramGb} GB RAM · {m.diskGb} GB disk
                  </option>
                ))}
            </select>
          </label>
          <Banner tone="info">External providers (Devin, OpenAI/ChatGPT) are available in Phase 3. PII masking is always applied.</Banner>
        </>
      )}
      {section === 'email' && (
        <>
          <h2>Email</h2>
          <label className="field">
            Transport
            <select value="Outlook COM" disabled>
              <option>Outlook COM</option>
            </select>
          </label>
          <span className="small muted">SMTP is available in Phase 2.</span>
          <label className="field">
            SPOC-1
            <input value={form.spoc1} onChange={(e) => set({ spoc1: e.target.value })} />
          </label>
          <label className="field">
            SPOC-2
            <input value={form.spoc2} onChange={(e) => set({ spoc2: e.target.value })} />
          </label>
        </>
      )}
      {section === 'template' && (
        <>
          <h2>Email template</h2>
          <span className="small muted">Placeholders: {EMAIL_PLACEHOLDERS.join(' ')}</span>
          <textarea rows={14} aria-label="Email template" value={form.emailTemplate} onChange={(e) => set({ emailTemplate: e.target.value })} />
          {unknown.length > 0 && <Banner tone="warning">Unknown placeholder(s): {unknown.join(', ')}</Banner>}
        </>
      )}
      {section === 'display' && (
        <>
          <h2>Display</h2>
          <label className="field">
            Density
            <select value={form.density} onChange={(e) => set({ density: e.target.value as PersonalSettings['density'] })}>
              <option>Comfortable</option>
              <option>Compact</option>
            </select>
          </label>
        </>
      )}
      {footer}
      <span className="small muted">Saving settings is recorded in the audit log.</span>
    </section>
  )
}

function Account() {
  const { user, setUser } = useAuth()
  if (!user) return null
  return (
    <section className="card stack">
      <h2>Account</h2>
      <div className="small">
        <div>Name: {user.fullName}</div>
        <div>Corporate email: {user.email}</div>
        <div>Role: {user.role}</div>
      </div>
      <h3>Change password</h3>
      <ChangePasswordForm user={user} onDone={setUser} />
    </section>
  )
}

/** Settings (GUI_OCC_DESIGN.md §15). OCC-only configuration is not shown here (REQ-ROLE-04). */
export function SettingsPage() {
  return (
    <div className="page">
      <h1>Settings</h1>
      <div className="subnav-layout">
        <nav className="subnav" aria-label="Settings sections">
          {SECTIONS.map(([id, label]) => (
            <NavLink key={id} to={`/settings/${id}`}>
              {label}
            </NavLink>
          ))}
        </nav>
        <Routes>
          <Route index element={<Navigate to="account" replace />} />
          <Route path="account" element={<Account />} />
          <Route path="data" element={<FormSection section="data" />} />
          <Route path="llm" element={<FormSection section="llm" />} />
          <Route path="email" element={<FormSection section="email" />} />
          <Route path="template" element={<FormSection section="template" />} />
          <Route path="package" element={<SettingsPackagePanel />} />
          <Route path="display" element={<FormSection section="display" />} />
        </Routes>
      </div>
    </div>
  )
}
