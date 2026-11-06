import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { CandidateDetail, CvFact, CvFacts, CvVersion, IdentityResolution } from '../api/types'
import { BandChip, Banner, Chip, EligibilityBadge, ErrorState, MaskedValue, MatchDoughnut, Modal, Spinner } from '../components/ui'
import { formatDate, formatDateTime, initials, orNotRecorded } from '../lib/format'

type Tab = 'overview' | 'cv' | 'skills' | 'matches' | 'activity'

const NOT_FOUND = <span className="muted">Not found in CV</span>

function FactItem({ fact }: { fact: CvFact }) {
  return (
    <div className="stack" style={{ gap: 2 }}>
      <span>
        {fact.value} <Chip tone={fact.source === 'llm' ? 'info' : 'neutral'}>{fact.source === 'llm' ? 'Local AI' : 'Rule'}</Chip>
      </span>
      <details className="small muted">
        <summary>Evidence</summary>
        <blockquote style={{ margin: '4px 0 0 12px', whiteSpace: 'pre-wrap' }}>{fact.evidence}</blockquote>
      </details>
    </div>
  )
}

/** Structured CV facts (REQ-IN-21/23): only values grounded in the CV text, each with its quote; nothing is inferred. */
function CvFactsCard({ cv, facts }: { cv: CvVersion; facts: CvFacts }) {
  const single = (label: string, f: CvFact | null) => (
    <div>
      <strong className="small">{label}</strong>
      <div>{f ? <FactItem fact={f} /> : NOT_FOUND}</div>
    </div>
  )
  const list = (label: string, items: CvFact[]) => (
    <div>
      <strong className="small">{label}</strong>
      {items.length === 0 ? <div>{NOT_FOUND}</div> : items.map((f, i) => <FactItem key={`${f.span[0]}-${i}`} fact={f} />)}
    </div>
  )
  return (
    <section className="card stack" aria-label={`CV facts for CV-${cv.cvVersionId}`}>
      <h3>
        CV facts — {cv.fileName} <span className="small muted mono">CV-{cv.cvVersionId}</span>
      </h3>
      {single('Name', facts.name)}
      {single('Phone (masked)', facts.phone)}
      {list('Experience', facts.experience)}
      {list('Qualifications', facts.qualifications)}
      {list('Projects', facts.projects)}
    </section>
  )
}

/**
 * Incomplete Candidate Information prompt (REQ-IN-23/25, GUI_OCC_DESIGN.md §12.3). Default is
 * "Generate internal ID and continue"; emails may only be selected from CT records (GQ-06).
 */
export function IncompleteIdentityDialog({ detail, onClose }: { detail: CandidateDetail; onClose: () => void }) {
  const qc = useQueryClient()
  const [action, setAction] = useState<IdentityResolution['action']>('generate_internal_id')
  const [resourceId, setResourceId] = useState<number | undefined>(undefined)
  const m = useMutation({
    mutationFn: () => api.resolveIdentity(detail.candidateId, { action, resourceId }),
    onSuccess: () => {
      void qc.invalidateQueries()
      onClose()
    },
  })
  return (
    <Modal
      title="Incomplete Candidate Information"
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={action === 'select_ct_record' && !resourceId} onClick={() => m.mutate()}>
            Continue
          </button>
        </>
      }
    >
      <p>
        The CV for “{orNotRecorded(detail.resource.name)}” has no confirmed email address
        {detail.ctEmailOptions.length > 0 && `, and the name matches ${detail.ctEmailOptions.length} other candidate(s) in the Candidate Tracker`}.
      </p>
      <fieldset className="stack" style={{ border: 'none', padding: 0 }}>
        <legend>How do you want to continue?</legend>
        <label className="row">
          <input type="radio" name="identity" checked={action === 'select_ct_record'} onChange={() => setAction('select_ct_record')} disabled={detail.ctEmailOptions.length === 0} />
          Select the matching Candidate Tracker record (email is copied from the CT, never typed)
        </label>
        {action === 'select_ct_record' && (
          <div className="stack" style={{ paddingLeft: 24 }}>
            {detail.ctEmailOptions.map((o) => (
              <label key={o.resourceId} className="row small">
                <input type="radio" name="ct" checked={resourceId === o.resourceId} onChange={() => setResourceId(o.resourceId)} />
                {o.label} · <span className="mono">{o.emailMasked}</span>
              </label>
            ))}
          </div>
        )}
        <label className="row">
          <input type="radio" name="identity" checked={action === 'generate_internal_id'} onChange={() => setAction('generate_internal_id')} />
          Generate internal ID and continue (default)
        </label>
        <label className="row">
          <input type="radio" name="identity" checked={action === 'review_later'} onChange={() => setAction('review_later')} />
          Review later
        </label>
      </fieldset>
      {m.isError && <ErrorState error={m.error} />}
    </Modal>
  )
}

function Picker() {
  const resources = useQuery({ queryKey: ['resources'], queryFn: () => api.listResources() })
  const [q, setQ] = useState('')
  const navigate = useNavigate()
  const rows = (resources.data ?? []).filter((r) => `${r.name} ${r.shortName}`.toLowerCase().includes(q.toLowerCase())).slice(0, 12)
  return (
    <div className="page">
      <h1>Candidate Profile</h1>
      <div className="card stack">
        <input aria-label="Search candidates" placeholder="Search by name or ShortName…" value={q} onChange={(e) => setQ(e.target.value)} />
        <ul className="list-plain">
          {rows.map((r) => (
            <li key={r.resourceId} className="queue-item" onClick={() => {
                if (r.candidateId) navigate(`/candidates/${r.candidateId}`)
              }}>
              <span className="avatar">{initials(r.name ?? r.shortName)}</span>
              <span style={{ flex: 1 }}>
                {orNotRecorded(r.name)} · <span className="mono">{r.shortName}</span> · {r.grade ?? '—'}
              </span>
              <EligibilityBadge eligibility={r.eligibility} reasons={r.exclusionReasons} />
            </li>
          ))}
        </ul>
        <Link to="/bench">Browse Bench List</Link>
      </div>
    </div>
  )
}

/** Candidate Profile (REQ-IN-22..25, REQ-OUT-02/03, REQ-MAT-08, GUI_OCC_DESIGN.md §12). */
export function CandidateProfilePage() {
  const { candidateId } = useParams()
  const id = Number(candidateId)
  const detail = useQuery({ queryKey: ['candidate', id], queryFn: () => api.getCandidate(id), enabled: Number.isFinite(id) && id > 0 })
  const [tab, setTab] = useState<Tab>('overview')
  const [prompt, setPrompt] = useState<boolean | null>(null)
  if (!candidateId) return <Picker />
  if (detail.isLoading) return <div className="page"><Spinner /></div>
  if (detail.isError || !detail.data) return <div className="page"><ErrorState error={detail.error} onRetry={() => void detail.refetch()} /></div>
  const d = detail.data
  const r = d.resource
  const incomplete = d.identityStatus === 'ambiguous' || d.identityStatus === 'incomplete'
  const showPrompt = prompt ?? d.identityStatus === 'ambiguous'
  return (
    <div className="page">
      <div className="card row" style={{ alignItems: 'flex-start' }}>
        <span className="avatar" style={{ width: 48, height: 48, fontSize: 16 }}>
          {initials(r.name ?? r.shortName)}
        </span>
        <div className="stack" style={{ gap: 4, flex: 1 }}>
          <h1>
            {orNotRecorded(r.name)} · <span className="mono">{r.shortName}</span>
          </h1>
          <div className="row small">
            <MaskedValue masked={r.emailAddressMasked} entity="resource" id={r.resourceId} /> · Phone{' '}
            <MaskedValue masked={d.phoneMasked} entity="resource" id={r.resourceId} field="phone" /> · {orNotRecorded(r.grade)} · {r.location}
          </div>
          <div className="row small">
            <EligibilityBadge eligibility={r.eligibility} reasons={r.exclusionReasons} /> · {r.benchStatus} · {r.groupName} (Owner: {r.groupOwner}) ·{' '}
            <span className="mono">{d.internalRef}</span>
            {incomplete && (
              <button className="btn btn-sm" onClick={() => setPrompt(true)}>
                Resolve identity
              </button>
            )}
          </div>
        </div>
        <Link className="btn" to={`/matches?req=`}>
          Open in Match Results
        </Link>
      </div>
      {r.eligibility === 'excluded' && <Banner tone="danger">Excluded — {r.exclusionReasons.join('; ')}. Not compared with JDs.</Banner>}
      <div className="tabs" role="tablist">
        {(
          [
            ['overview', 'Overview'],
            ['cv', `CV versions (${d.cvVersions.length})`],
            ['skills', 'Skills & evidence'],
            ['matches', 'Matches & score history'],
            ['activity', 'Activity'],
          ] as [Tab, string][]
        ).map(([t, label]) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {label}
          </button>
        ))}
      </div>
      {tab === 'overview' && (
        <div className="grid-12">
          <section className="card span-8">
            <h3>CT data (as uploaded)</h3>
            <table className="data">
              <tbody>
                {(
                  [
                    ['ShortName', r.shortName],
                    ['Grade', orNotRecorded(r.grade)],
                    ['Location', orNotRecorded(r.location)],
                    ['Bench Status', orNotRecorded(r.benchStatus)],
                    ['Skills', r.skills.join(', ')],
                    ['Available Capacity (display only)', r.availableCapacity ?? 'Not recorded'],
                    ['Bench Start', formatDate(r.benchStartDate)],
                    ['Bench End', formatDate(r.benchEndDate)],
                    ['Group / Owner', `${r.groupName ?? '—'} / ${r.groupOwner ?? '—'}`],
                    ['Candidate type', `${r.candidateType} (domain is a heuristic, not proof of employment)`],
                  ] as [string, string][]
                ).map(([k, v]) => (
                  <tr key={k}>
                    <th style={{ position: 'static', width: 240 }}>{k}</th>
                    <td>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <section className="card span-4 stack">
            <h3>Eligibility</h3>
            {r.exclusionReasons.length === 0 ? (
              <ul className="small">
                <li>✓ Status {r.benchStatus} {r.benchStatusPriority ? `(priority ${r.benchStatusPriority})` : ''}</li>
                <li>✓ Group {r.groupName} (not “Not on Bench”)</li>
                <li>✓ Bench End Date blank</li>
                <li>✓ Grade recorded</li>
              </ul>
            ) : (
              <ul className="small">{r.exclusionReasons.map((x) => <li key={x}>✕ {x}</li>)}</ul>
            )}
            <span className="small muted">ⓘ Blank Bench End Date is not confirmation to submit (REQ-ELIG-05).</span>
          </section>
        </div>
      )}
      {tab === 'cv' && (
        <section className="card">
          {d.cvVersions.length === 0 ? (
            <p className="muted">No CV found in the resume folder. Matching uses CT skills only.</p>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Folder</th>
                  <th>Modified</th>
                  <th>Extracted</th>
                  <th>Resume ID</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {d.cvVersions.map((c) => (
                  <tr key={c.cvVersionId}>
                    <td>{c.fileName}</td>
                    <td className="small mono">{c.folderPath}</td>
                    <td>{formatDate(c.modifiedAt)}</td>
                    <td>{formatDateTime(c.extractedAt)}</td>
                    <td className="mono">CV-{c.cvVersionId}</td>
                    <td>{c.isBest && <Chip tone="success">Best / latest</Chip>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
      {tab === 'cv' &&
        d.cvVersions.map((c) =>
          c.facts ? (
            <CvFactsCard key={c.cvVersionId} cv={c} facts={c.facts} />
          ) : (
            <p key={c.cvVersionId} className="small muted">
              CV-{c.cvVersionId}: synced before CV facts were extracted, so no facts are shown.
            </p>
          ),
        )}
      {tab === 'skills' && (
        <section className="card stack">
          <h3>CT skills</h3>
          <div className="row">{r.skills.map((s) => <Chip key={s} tone="info">{s}</Chip>)}</div>
          <span className="small muted">Provenance: Candidate Tracker “Skills (comma separated)” cell. Items not recorded require validation.</span>
        </section>
      )}
      {tab === 'matches' && (
        <section className="card">
          {d.matches.length === 0 ? (
            <p className="muted">No matches in the latest run.</p>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>Requisition</th>
                  <th>Role</th>
                  <th>Rank</th>
                  <th>Match %</th>
                  <th>Band</th>
                  <th>Run / template</th>
                </tr>
              </thead>
              <tbody>
                {d.matches.map((m) => (
                  <tr key={m.resultId}>
                    <td className="mono">
                      <Link to={`/matches?req=${m.codeRequisition}`}>{m.codeRequisition}</Link>
                    </td>
                    <td>{m.demandRequestName}</td>
                    <td>#{m.rank}</td>
                    <td>
                      <MatchDoughnut score={m.score} band={m.band} />
                    </td>
                    <td>
                      <BandChip band={m.band} />
                    </td>
                    <td className="small">Run {m.runId}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
      {tab === 'activity' && (
        <section className="card">
          {d.activity.length === 0 ? (
            <p className="muted">No recorded activity yet.</p>
          ) : (
            <ul className="small">
              {d.activity.map((a, i) => (
                <li key={i}>
                  {formatDateTime(a.at)} — {a.summary}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      {showPrompt && incomplete && <IncompleteIdentityDialog detail={d} onClose={() => setPrompt(false)} />}
    </div>
  )
}
