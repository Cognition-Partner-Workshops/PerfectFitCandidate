import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Send } from 'lucide-react'
import { api, ApiError } from '../api'
import type { EmailDraft, EmailStatus } from '../api/types'
import { BandChip, Banner, Chip, EmptyState, ErrorState, MatchDoughnut, Modal, Spinner } from '../components/ui'
import { downloadBlob } from '../lib/download'
import { groupByRequisition, hoursLeft } from '../lib/emailSelection'
import { EMAIL_PLACEHOLDERS } from '../lib/emailTemplate'

const TABS: (EmailStatus | 'All')[] = ['Draft', 'Approved', 'Sent', 'Failed', 'All']

function statusTone(s: EmailStatus) {
  return s === 'Sent' ? 'success' : s === 'Failed' ? 'danger' : s === 'Approved' ? 'info' : 'neutral'
}

/**
 * Email Approval (REQ-EML-01..07, GUI_OCC_DESIGN.md §13): top 4 of 5 pre-selected per requisition,
 * explicit per-profile approval before Outlook handoff or COM send, duplicate-send guard.
 */
export function EmailApprovalPage() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['emails'], queryFn: () => api.listEmails() })
  const [tab, setTab] = useState<EmailStatus | 'All'>('Draft')
  const [activeId, setActiveId] = useState<number | null>(null)
  const [checked, setChecked] = useState<Set<number> | null>(null)
  const [confirmBulk, setConfirmBulk] = useState(false)
  const [resendFor, setResendFor] = useState<EmailDraft | null>(null)
  const [edits, setEdits] = useState<Record<number, { subject: string; body: string }>>({})
  const [error, setError] = useState<string | null>(null)

  const items = useMemo(() => list.data?.items ?? [], [list.data])
  const visible = tab === 'All' ? items : items.filter((e) => e.status === tab)
  const preselected = useMemo(() => new Set(items.filter((e) => e.preselected && e.status === 'Draft').map((e) => e.emailId)), [items])
  const selected = checked ?? preselected
  const active = items.find((e) => e.emailId === activeId) ?? visible[0]
  const counts = Object.fromEntries(TABS.map((t) => [t, t === 'All' ? items.length : items.filter((e) => e.status === t).length]))

  const refresh = () => qc.invalidateQueries({ queryKey: ['emails'] })
  const onErr = (e: unknown) => {
    if (e instanceof ApiError && e.code === 'EMAIL_DUPLICATE_PAIR') return
    setError(e instanceof Error ? e.message : 'Action failed')
  }
  const approve = useMutation({
    mutationFn: async ({ id, resend }: { id: number; resend?: boolean }) => {
      if (edits[id]) await api.updateEmail(id, edits[id])
      return api.approveEmail(id, resend)
    },
    onSuccess: () => void refresh(),
    onError: (e, vars) => {
      if (e instanceof ApiError && e.code === 'EMAIL_DUPLICATE_PAIR') setResendFor(items.find((x) => x.emailId === vars.id) ?? null)
      onErr(e)
    },
  })
  const send = useMutation({ mutationFn: (id: number) => api.sendEmail(id), onSuccess: () => void refresh(), onError: onErr })
  const handoff = useMutation({
    mutationFn: async (id: number) => {
      const result = await api.openEmailInOutlook(id)
      if (!result.opened) {
        const file = await api.downloadEmailDraft(id)
        downloadBlob(file.blob, file.fileName)
      }
      return result.draft
    },
    onSuccess: () => void refresh(),
    onError: onErr,
  })
  const confirmHandoff = useMutation({
    mutationFn: ({ id, sent }: { id: number; sent: boolean }) => api.confirmEmailHandoff(id, sent),
    onSuccess: () => void refresh(),
    onError: onErr,
  })
  const save = useMutation({
    mutationFn: (e: EmailDraft) => api.updateEmail(e.emailId, edits[e.emailId] ?? { subject: e.subject, body: e.body }),
    onSuccess: () => void refresh(),
    onError: onErr,
  })
  const bulkApprove = async () => {
    setConfirmBulk(false)
    for (const id of selected) {
      const e = items.find((x) => x.emailId === id)
      if (e?.status === 'Draft') {
        try {
          await api.approveEmail(id)
        } catch (err) {
          onErr(err)
        }
      }
    }
    void refresh()
  }

  if (list.isLoading) return <div className="page"><Spinner /></div>
  if (list.isError) return <div className="page"><ErrorState error={list.error} onRetry={() => void list.refetch()} /></div>
  const transport = list.data?.transport
  const draftFileTransport = transport?.transport === 'Outlook draft file'
  const draft = active ? (edits[active.emailId] ?? { subject: active.subject, body: active.body }) : null
  const unresolved = draft ? EMAIL_PLACEHOLDERS.filter((p) => draft.body.includes(p) || draft.subject.includes(p)) : []
  const selectedDrafts = items.filter((e) => selected.has(e.emailId) && e.status === 'Draft')

  return (
    <div className="page">
      <div className="page-header">
        <h1>Email Approval</h1>
        <span className="meta">
          {draftFileTransport
            ? 'Transport: Outlook draft file'
            : `Transport: ${transport?.transport} (${transport?.outlookDetected ? 'detected ●' : 'not detected'})`}
        </span>
        <span className="spacer" />
        <span className="meta">
          Draft {counts.Draft} · Approved {counts.Approved} · Sent {counts.Sent} · Failed {counts.Failed}
        </span>
      </div>
      {!draftFileTransport && !transport?.outlookDetected && <Banner tone="warning">Outlook isn't running or automation is blocked. Start Outlook and retry.</Banner>}
      {error && <Banner tone="danger">{error}</Banner>}
      {items.length === 0 ? (
        <EmptyState message="No emails awaiting approval. Select candidates in Match Results and choose Send to Email Approval." />
      ) : (
        <div className="two-pane">
          <section className="card stack" aria-label="Approval queue">
            <div className="tabs" role="tablist">
              {TABS.map((t) => (
                <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
                  {t} ({counts[t]})
                </button>
              ))}
            </div>
            {groupByRequisition(visible).map(([req, emails]) => {
              const left = hoursLeft(emails[0]?.demandUploadedAt ?? '', new Date())
              const overdue = left < 0 && !emails.every((e) => e.status === 'Sent')
              return (
                <div key={req}>
                  <div className="row small" style={{ fontWeight: 600 }}>
                    <span className="mono">{req}</span> · {emails[0]?.accountName}
                    <span className="spacer" />
                    {overdue ? <Chip tone="danger">⚠ overdue</Chip> : left >= 0 ? <Chip tone="info">⏱ {left} h left</Chip> : null}
                  </div>
                  <ul className="list-plain">
                    {emails.map((e) => (
                      <li key={e.emailId} className={`queue-item${active?.emailId === e.emailId ? ' active' : ''}`} onClick={() => setActiveId(e.emailId)}>
                        <input
                          type="checkbox"
                          aria-label={`Select ${e.candidateName ?? 'candidate'} (${req})`}
                          checked={selected.has(e.emailId)}
                          disabled={e.status !== 'Draft'}
                          onClick={(ev) => ev.stopPropagation()}
                          onChange={() => {
                            const next = new Set(selected)
                            if (next.has(e.emailId)) next.delete(e.emailId)
                            else next.add(e.emailId)
                            setChecked(next)
                          }}
                        />
                        <span className="num">#{e.rank}</span>
                        <span style={{ flex: 1 }}>{e.candidateName ?? 'Not recorded'}</span>
                        <span className="num small">{Math.round(e.score)} %</span>
                        <Chip tone={statusTone(e.status)}>
                          {draftFileTransport && e.status === 'Approved' && e.handoffAt
                            ? 'Awaiting confirmation'
                            : e.status}
                        </Chip>
                      </li>
                    ))}
                  </ul>
                </div>
              )
            })}
            <button className="btn" disabled={selectedDrafts.length === 0} onClick={() => setConfirmBulk(true)}>
              Approve selected ({selectedDrafts.length})
            </button>
          </section>
          {active && draft && (
            <section className="card stack" aria-label="Draft editor">
              <div className="row">
                <h2>
                  <span className="mono">{active.codeRequisition}</span> · {active.demandRequestName}
                </h2>
                <span className="spacer" />
                <Chip tone={statusTone(active.status)}>Status: {active.status}</Chip>
              </div>
              <div className="row">
                Candidate: <strong>{active.candidateName}</strong> <MatchDoughnut score={active.score} band={active.band} /> <BandChip band={active.band} />
              </div>
              <div className="row small">
                <strong>To</strong> <span className="mono">{active.toMasked ?? 'Not recorded'}</span>
              </div>
              <div className="row small">
                <strong>CC</strong> {active.cc.length ? active.cc.map((c) => <Chip key={c}>{c}</Chip>) : <span className="muted">No resource-team recipients</span>}
              </div>
              <label className="field">
                Subject
                <input
                  value={draft.subject}
                  disabled={active.status !== 'Draft'}
                  onChange={(e) => setEdits({ ...edits, [active.emailId]: { ...draft, subject: e.target.value } })}
                />
              </label>
              <label className="field">
                Body
                <textarea
                  rows={12}
                  value={draft.body}
                  disabled={active.status !== 'Draft'}
                  onChange={(e) => setEdits({ ...edits, [active.emailId]: { ...draft, body: e.target.value } })}
                />
              </label>
              {unresolved.length > 0 && <Banner tone="warning">Unresolved placeholder(s): {unresolved.join(', ')}</Banner>}
              <span className="small muted">Attachment: JD_{active.codeRequisition}.pdf (generated)</span>
              {active.failureReason && <Banner tone="danger">Send failed: {active.failureReason}</Banner>}
              {draftFileTransport && active.status === 'Approved' && active.handoffAt && (
                <Banner tone="info">
                  Opened in Outlook at {new Date(active.handoffAt).toLocaleString()}. Check the draft, click Send in Outlook, then confirm here.
                </Banner>
              )}
              <div className="modal-actions">
                {active.status === 'Draft' && (
                  <>
                    <button className="btn" onClick={() => save.mutate(active)} disabled={!edits[active.emailId]}>
                      Save
                    </button>
                    <button className="btn btn-primary" onClick={() => {
                        setActiveId(active.emailId)
                        approve.mutate({ id: active.emailId })
                      }} aria-label={`Approve email to ${active.candidateName ?? 'candidate'}`}>
                      <Check size={16} /> Approve
                    </button>
                  </>
                )}
                {draftFileTransport ? (
                  active.status === 'Approved' || active.status === 'Failed' ? (
                    active.handoffAt ? (
                      <>
                        <button
                          className="btn btn-primary"
                          onClick={() => confirmHandoff.mutate({ id: active.emailId, sent: true })}
                        >
                          <Check size={16} /> Mark as sent
                        </button>
                        <button
                          className="btn"
                          onClick={() => confirmHandoff.mutate({ id: active.emailId, sent: false })}
                        >
                          Not sent
                        </button>
                        <button className="btn" onClick={() => handoff.mutate(active.emailId)}>
                          Open again
                        </button>
                      </>
                    ) : (
                      <button className="btn btn-primary" onClick={() => handoff.mutate(active.emailId)}>
                        Open in Outlook
                      </button>
                    )
                  ) : null
                ) : (
                  <button
                    className="btn btn-primary"
                    disabled={!(active.status === 'Approved' || active.status === 'Failed') || !transport?.outlookDetected}
                    title={active.status === 'Approved' || active.status === 'Failed' ? undefined : 'Approve this email before sending'}
                    onClick={() => send.mutate(active.emailId)}
                  >
                    <Send size={16} /> {active.status === 'Failed' ? 'Retry send' : 'Send'}
                  </button>
                )}
              </div>
              <p className="small muted">Approvals, handoffs, sends and analyst confirmations are recorded in the audit log. Replies are not tracked.</p>
            </section>
          )}
        </div>
      )}
      {confirmBulk && (
        <Modal
          title={`Approve ${selectedDrafts.length} email(s)?`}
          onClose={() => setConfirmBulk(false)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirmBulk(false)}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={() => void bulkApprove()}>
                Approve all listed
              </button>
            </>
          }
        >
          <p>Each email below gets its own Approved state. Nothing is sent until you choose Send.</p>
          <ul>
            {selectedDrafts.map((e) => (
              <li key={e.emailId}>
                <span className="mono">{e.codeRequisition}</span> · #{e.rank} {e.candidateName} · <span className="mono">{e.toMasked}</span>
              </li>
            ))}
          </ul>
        </Modal>
      )}
      {resendFor && (
        <Modal
          title="Already sent"
          onClose={() => setResendFor(null)}
          actions={
            <>
              <button className="btn" onClick={() => setResendFor(null)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                onClick={() => {
                  approve.mutate({ id: resendFor.emailId, resend: true })
                  setResendFor(null)
                }}
              >
                Re-send
              </button>
            </>
          }
        >
          <p>
            This candidate–demand email was already sent on {resendFor.previouslySentAt?.slice(0, 10)}. Re-send?
          </p>
        </Modal>
      )}
    </div>
  )
}
