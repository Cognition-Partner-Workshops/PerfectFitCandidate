import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { EmailTemplateDraft } from '../../api/types'
import { Banner, Chip, EmptyState, ErrorState, Spinner } from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { EMAIL_PLACEHOLDERS, checkTemplate } from '../../lib/occ'

/** Versioned candidate email templates (REQ-EML-03, GUI_OCC_DESIGN.md §16.3). Versions are append-only. */
export function OccEmailTemplates() {
  const qc = useQueryClient()
  const [all, setAll] = useState(false)
  const list = useQuery({ queryKey: ['occ', 'email-templates', all], queryFn: () => api.listEmailTemplates(all) })
  const [draft, setDraft] = useState<(EmailTemplateDraft & { isNew: boolean }) | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const refresh = () => void qc.invalidateQueries({ queryKey: ['occ', 'email-templates'] })
  const open = useMutation({
    mutationFn: (id: number) => api.getEmailTemplate(id),
    onSuccess: (t) => setDraft({ name: t.name, subject: t.subject, bodyHtml: t.bodyHtml, isNew: false }),
  })
  const create = useMutation({
    // With no default, the first email draft inserts candidate_default v1, which collides with a saved candidate_default.
    mutationFn: async (d: EmailTemplateDraft) => {
      const t = await api.createEmailTemplate(d)
      try {
        const hasDefault = (await api.listEmailTemplates(true)).some((x) => x.isDefault)
        return { t: hasDefault ? t : await api.setDefaultEmailTemplate(t.emailTemplateId), warn: null }
      } catch (e) {
        return { t, warn: e instanceof Error ? e.message : 'Request failed' }
      }
    },
    onSuccess: ({ t, warn }) => {
      setDraft(null)
      setNotice(
        warn
          ? `${t.name} v${t.version} saved, but the default could not be checked or set (${warn}). Set a default before the first email draft.`
          : t.isDefault
          ? `${t.name} v${t.version} saved and set as the default template, because no default existed.`
          : `${t.name} v${t.version} saved. Set it as default to use it for new email drafts.`,
      )
      refresh()
    },
  })
  const makeDefault = useMutation({
    mutationFn: (id: number) => api.setDefaultEmailTemplate(id),
    onSuccess: (t) => {
      setNotice(`${t.name} v${t.version} is now the default template.`)
      refresh()
    },
  })
  if (list.isLoading) return <Spinner />
  if (list.isError) return <ErrorState error={list.error} />
  const rows = list.data ?? []
  const name = draft?.name.trim() ?? ''
  const nextVersion = Math.max(0, ...rows.filter((r) => r.name === name).map((r) => r.version)) + 1
  const taken = Boolean(draft?.isNew && rows.some((r) => r.name === name))
  const { unknown, malformed } = draft ? checkTemplate(draft.subject, draft.bodyHtml) : { unknown: [], malformed: false }
  const canSave = Boolean(draft && name && draft.subject.trim() && draft.bodyHtml.trim() && !taken && unknown.length === 0 && !malformed)
  return (
    <section className="card stack" aria-label="Email templates">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h2>Email templates</h2>
        <button className="btn" onClick={() => setDraft({ name: '', subject: '', bodyHtml: '', isNew: true })}>
          New template
        </button>
      </div>
      {notice && <Banner tone="success">{notice}</Banner>}
      {[open, create, makeDefault].map((m, i) => m.isError && <ErrorState key={i} error={m.error} />)}
      <label className="row small">
        <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> Show all versions
      </label>
      {rows.length === 0 ? (
        <EmptyState message="No email templates yet. The first template you save becomes the default; otherwise the first email draft creates candidate_default v1." />
      ) : (
        <table className="data" aria-label="Email template versions">
          <thead>
            <tr>
              <th>Name</th>
              <th>Version</th>
              <th>Subject</th>
              <th>Default</th>
              <th>Created</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.emailTemplateId}>
                <td className="mono">{r.name}</td>
                <td>v{r.version}</td>
                <td>{r.subject}</td>
                <td>{r.isDefault && <Chip tone="success">Default</Chip>}</td>
                <td>{formatDateTime(r.createdAt)}</td>
                <td className="row">
                  <button className="btn btn-sm" aria-label={`Edit ${r.name} v${r.version}`} onClick={() => open.mutate(r.emailTemplateId)}>
                    Edit
                  </button>
                  <button
                    className="btn btn-sm"
                    aria-label={`Set ${r.name} v${r.version} as default`}
                    disabled={r.isDefault || makeDefault.isPending}
                    onClick={() => makeDefault.mutate(r.emailTemplateId)}
                  >
                    Set as default
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {draft && (
        <div className="card stack" aria-label="Template editor">
          <h3>{draft.isNew ? 'New template' : `${draft.name} → v${nextVersion}`}</h3>
          <label className="field">
            Template name
            <input value={draft.name} disabled={!draft.isNew} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </label>
          {taken && <span className="error-text">A template with this name exists; edit it to add a version.</span>}
          <label className="field">
            Subject
            <input value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} />
          </label>
          <label className="field">
            Body (HTML)
            <textarea rows={10} className="mono" value={draft.bodyHtml} onChange={(e) => setDraft({ ...draft, bodyHtml: e.target.value })} />
          </label>
          <span className="small muted">Placeholders: {EMAIL_PLACEHOLDERS.map((p) => `{${p}}`).join(' ')}</span>
          {unknown.length > 0 && <span className="error-text">Unknown placeholder: {unknown.map((p) => `{${p}}`).join(', ')}</span>}
          {malformed && <span className="error-text">Unmatched or formatted brace. Write a literal brace as {'{{'} or {'}}'}.</span>}
          <div className="row">
            <button
              className="btn btn-primary"
              disabled={!canSave || create.isPending}
              onClick={() => create.mutate({ name, subject: draft.subject, bodyHtml: draft.bodyHtml })}
            >
              {draft.isNew ? 'Create template' : `Save as v${nextVersion}`}
            </button>
            <button className="btn" onClick={() => setDraft(null)}>
              Cancel
            </button>
          </div>
          <span className="small muted">Saving adds a new version; earlier versions are kept. New email drafts use the default version.</span>
        </div>
      )}
    </section>
  )
}
