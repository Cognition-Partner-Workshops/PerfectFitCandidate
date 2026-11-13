import { useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Upload, X } from 'lucide-react'
import { api, ApiError } from '../api'
import type { Demand, JdRequirementItem, JdRequirements, JdStatus } from '../api/types'
import { BandChip, Banner, Chip, EmptyState, ErrorState, Modal, Spinner } from '../components/ui'
import { UploadDialog } from '../components/UploadDialog'
import { SummaryCard } from '../components/SummaryCard'
import { summarizeCurrentResults } from '../lib/currentResults'
import { formatDate, roleLocation } from '../lib/format'

const STATUS_TONE: Record<JdStatus, 'neutral' | 'info' | 'success' | 'warning'> = {
  'Not analysed': 'neutral',
  Draft: 'info',
  Confirmed: 'success',
  'JD changed': 'warning',
}

/** No requirements exist for the current JD version yet, so the drawer must analyze first. */
const needsAnalysis = (s: JdStatus) => s === 'Not analysed' || s === 'JD changed'

const persistedText = (kind: JdRequirementItem['kind'], text: string) =>
  kind === 'qualification' || kind === 'domain' ? text.trim() : text
const findItem = (items: JdRequirementItem[] | undefined, kind: JdRequirementItem['kind'], text: string) =>
  items?.find((item) => item.kind === kind && persistedText(kind, item.text) === persistedText(kind, text))

function RequirementMetadata({ item }: { item?: JdRequirementItem }) {
  const moved = item?.classificationReason === 'classification_adjusted'
    ? item.kind === 'preferred_skill' ? 'Preferred' : item.kind === 'mandatory_skill' ? 'Mandatory' : null
    : null
  return <>
    {item?.evidenceUnverified && <span className="small" role="note">Evidence not found in JD text — check before confirming</span>}
    {moved && <span className="small muted" role="note">Moved to {moved} based on JD wording</span>}
  </>
}

function RequirementNotes({ items, kind, texts }: { items?: JdRequirementItem[]; kind: JdRequirementItem['kind']; texts: string[] }) {
  return texts.filter((text) => findItem(items, kind, text)?.evidenceUnverified).map((text, index) => (
    <div key={`${text}-${index}`} role="group" aria-label={`${kind}: ${text}`}>
      <span>{text}</span> <RequirementMetadata item={findItem(items, kind, text)} />
    </div>
  ))
}

function SkillChips({ label, skills, items, kind, onChange }: { label: string; skills: string[]; items?: JdRequirementItem[]; kind: JdRequirementItem['kind']; onChange: (s: string[]) => void }) {
  const [draft, setDraft] = useState('')
  return (
    <div className="stack" style={{ gap: 4 }}>
      <strong className="small">{label}</strong>
      <div className="row">
        {skills.map((s) => (
          <span key={s} className="stack" role="group" aria-label={`${label}: ${s}`}>
            <Chip tone="info">
              {s}
              <button aria-label={`Remove ${s}`} onClick={() => onChange(skills.filter((x) => x !== s))}>
                <X size={12} />
              </button>
            </Chip>
            <RequirementMetadata item={findItem(items, kind, s)} />
          </span>
        ))}
        <input
          aria-label={`Add ${label.toLowerCase()}`}
          placeholder="+ Add"
          value={draft}
          size={10}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && draft.trim()) {
              e.preventDefault()
              if (!skills.includes(draft.trim())) onChange([...skills, draft.trim()])
              setDraft('')
            }
          }}
        />
      </div>
    </div>
  )
}

/** JD review drawer: Analyze JD → review requirements → Confirm & Start (REQ-MAT-02, §9.3). */
function JdReview({ demand, onClose }: { demand: Demand; onClose: () => void }) {
  const qc = useQueryClient()
  // The row status is a snapshot from when the drawer opened; analysis turns it into Draft.
  const [jdChanged] = useState(demand.jdStatus === 'JD changed')
  const analyzed = useRef(false)
  const analyzeOnce = async () => {
    const draft = await api.analyzeDemand(demand.demandId)
    analyzed.current = true
    void qc.invalidateQueries({ queryKey: ['demands'] })
    return draft
  }
  // Each analysis stores a new draft revision, so analyze only when the current JD version has no requirements.
  const req = useQuery({
    queryKey: ['requirements', demand.demandId],
    queryFn: async () => {
      if (!analyzed.current && needsAnalysis(demand.jdStatus)) return analyzeOnce()
      try {
        return await api.getRequirements(demand.demandId)
      } catch (e) {
        // Safety net for a stale status: no requirements exist yet after all.
        if (analyzed.current || !(e instanceof ApiError && e.code === 'REQUIREMENTS_NOT_FOUND')) throw e
      }
      return analyzeOnce()
    },
  })
  const jd = useQuery({
    queryKey: ['demand', demand.demandId],
    queryFn: () => api.getDemand(demand.demandId),
  })
  const jdText = jd.data?.jdText ?? ''
  const [edit, setEdit] = useState<JdRequirements | null>(null)
  const value = edit ?? req.data
  const metadata = value?.items
  const changeSkills = (kind: 'mandatory_skill' | 'preferred_skill', skills: string[]) => {
    if (!value) return
    const field = kind === 'mandatory_skill' ? 'mandatorySkills' : 'preferredSkills'
    setEdit({ ...value, [field]: skills })
  }
  const done = () => {
    void qc.invalidateQueries()
    onClose()
  }
  const save = useMutation({ mutationFn: (r: JdRequirements) => api.saveRequirements(r), onSuccess: done })
  const confirm = useMutation({
    mutationFn: async (r: JdRequirements) => {
      await api.saveRequirements(r)
      await api.confirmRequirements(r.demandId)
    },
    onSuccess: done,
  })
  return (
    <Modal
      title={`JD review — ${demand.codeRequisition}`}
      onClose={onClose}
      actions={
        value && (
          <>
            <span className="small muted spacer">Scoring template is read-only for Analysts.</span>
            <button className="btn" onClick={() => save.mutate(value)}>
              Save draft
            </button>
            <button className="btn btn-primary" disabled={value.mandatorySkills.length === 0} onClick={() => confirm.mutate(value)}>
              Confirm &amp; Start ▶
            </button>
          </>
        )
      }
    >
      <SummaryCard kind="demand" id={demand.demandId} />
      <span className="muted small">
        {demand.demandRequestName} · {demand.accountName} · {demand.codeLocalGrade} · DL region: {roleLocation(demand.region) || '—'} · Start{' '}
        {formatDate(demand.positionStartDate)}
      </span>
      {req.isError && !value ? (
        <ErrorState error={req.error} />
      ) : !value ? (
        <Spinner />
      ) : (
        <div className="grid-12">
          <div className="span-6">
            <strong className="small">Job description (normalised, read-only)</strong>
            {jd.isError ? (
              <ErrorState error={jd.error} />
            ) : jd.isLoading ? (
              <Spinner />
            ) : jdText.trim() ? (
              <pre className="card small" style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>
                {jdText}
              </pre>
            ) : (
              <span className="small muted">No JD text in the uploaded DL</span>
            )}
          </div>
          <div className="span-6 stack">
            {jdChanged && (
              <Banner tone="warning">
                <strong>The JD has changed since the last analysis.</strong> These requirements were extracted from the new JD; check them before
                Confirm &amp; Start.
              </Banner>
            )}
            {value.extractionMethod === 'rule_based_fallback' && (
              <Banner tone="warning">
                <strong>Extracted by rules, not the local AI model</strong> (reason: {value.fallbackReason ?? 'reason_not_recorded'}). Check every
                requirement carefully before Confirm &amp; Start.
              </Banner>
            )}
            {value.extractionMethod === 'llm' && <span className="small muted">Extracted by local AI model</span>}
            <SkillChips label="Mandatory skills" kind="mandatory_skill" items={metadata} skills={value.mandatorySkills} onChange={(s) => changeSkills('mandatory_skill', s)} />
            <SkillChips label="Preferred skills" kind="preferred_skill" items={metadata} skills={value.preferredSkills} onChange={(s) => changeSkills('preferred_skill', s)} />
            <label className="field">
              Experience (years)
              <input type="number" min={0} value={value.experienceYears ?? ''} onChange={(e) => setEdit({ ...value, experienceYears: e.target.value === '' ? null : Number(e.target.value) })} />
            </label>
            <RequirementNotes kind="experience" items={metadata} texts={value.experience ?? []} />
            <label className="field">
              Qualifications (separate with ;)
              <input value={value.qualifications} onChange={(e) => setEdit({ ...value, qualifications: e.target.value })} />
            </label>
            <RequirementNotes kind="qualification" items={metadata} texts={value.qualifications.split(';')} />
            <label className="field">
              Domain
              <input value={value.domain} onChange={(e) => setEdit({ ...value, domain: e.target.value })} />
            </label>
            <RequirementNotes kind="domain" items={metadata} texts={value.domain.split(';')} />
            <label className="field">
              Location (extracted from JD)
              <input value={value.location} placeholder="Not stated in the JD" onChange={(e) => setEdit({ ...value, location: e.target.value })} />
            </label>
            <label className="row small">
              <input type="checkbox" checked={value.mandatoryOnsite} onChange={(e) => setEdit({ ...value, mandatoryOnsite: e.target.checked })} /> Mandatory
              onsite (extracted from JD)
            </label>
            {value.synonymsApplied.length > 0 && <span className="small muted">Synonyms applied: {value.synonymsApplied.join(', ')}</span>}
            {/* The backend repeats the fallback as a warning; the banner above already shows it. */}
            {value.warnings
              .filter((w) => !(value.extractionMethod === 'rule_based_fallback' && w.startsWith('Rule-based fallback:')))
              .map((w) => (
                <Banner key={w} tone="warning">
                  {w}
                </Banner>
              ))}
          </div>
        </div>
      )}
      {(save.isError || confirm.isError) && <ErrorState error={save.error ?? confirm.error} />}
    </Modal>
  )
}

/** Demand List (REQ-ING-01..03, REQ-MAT-02, GUI_OCC_DESIGN.md §9). */
export function DemandListPage() {
  const qc = useQueryClient()
  const demands = useQuery({ queryKey: ['demands'], queryFn: () => api.listDemands() })
  const currentResults = useQuery({
    queryKey: ['current-results', 'rows'],
    queryFn: () => api.getCurrentResults(),
  })
  const [search, setSearch] = useState('')
  const [open, setOpen] = useState<Demand | null>(null)
  const [upload, setUpload] = useState(false)
  const analyze = useMutation({ mutationFn: (ids: number[]) => api.analyzeDemands(ids), onSuccess: () => void qc.invalidateQueries({ queryKey: ['demands'] }) })
  const currentMatches = useMemo(
    () => summarizeCurrentResults(currentResults.data ?? []),
    [currentResults.data],
  )
  const rows = useMemo(
    () =>
      (demands.data ?? [])
        .map((d) => ({
          ...d,
          matches: currentMatches.get(d.demandId)?.matches ?? 0,
          bestBand: currentMatches.get(d.demandId)?.bestBand ?? null,
        }))
        .filter((d) =>
          [d.codeRequisition, d.accountName, d.demandRequestName, d.bu, d.region].some((v) => (v ?? '').toLowerCase().includes(search.toLowerCase())),
        ),
    [currentMatches, demands.data, search],
  )
  const newOnes = (demands.data ?? []).filter((d) => needsAnalysis(d.jdStatus))
  if (demands.isLoading) return <div className="page"><Spinner /></div>
  if (demands.isError) return <div className="page"><ErrorState error={demands.error} onRetry={() => void demands.refetch()} /></div>
  return (
    <div className="page">
      <div className="page-header">
        <h1>Demand List</h1>
        <span className="meta">Last upload: DL_Nov.xlsx · 04 Nov 2026 09:12</span>
        <span className="spacer" />
        <Chip>Auto-sync: Not configured</Chip>
        <button className="btn" disabled={newOnes.length === 0} onClick={() => analyze.mutate(newOnes.map((d) => d.demandId))}>
          Analyze all new ({newOnes.length})
        </button>
        <button className="btn btn-primary" onClick={() => setUpload(true)}>
          <Upload size={16} /> Upload DL
        </button>
      </div>
      <div className="card">
        <input aria-label="Search demands" placeholder="Search…" value={search} onChange={(e) => setSearch(e.target.value)} />
      </div>
      {rows.length === 0 ? (
        <EmptyState message="No demands yet. Upload the Demand List (.xlsx)." />
      ) : (
        <div className="table-wrap">
          <table className="data" aria-label="Demands">
            <thead>
              <tr>
                <th>Code Requisition</th>
                <th>Position Start Date</th>
                <th>Account Name</th>
                <th>Code Local Grade</th>
                <th>Demand Request Name</th>
                <th>Region</th>
                <th>BU</th>
                <th>Owner</th>
                <th>JD status</th>
                <th>Matches</th>
                <th>Best band</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((d) => (
                <tr key={d.demandId}>
                  <td className="mono">
                    {d.codeRequisition} {!d.idFormatOk && <span title="Not in T1/T2XXXX format (warning)">⚠</span>}
                  </td>
                  <td>{formatDate(d.positionStartDate)}</td>
                  <td>{d.accountName}</td>
                  <td>{d.codeLocalGrade}</td>
                  <td>{d.demandRequestName}</td>
                  <td>{roleLocation(d.region)}</td>
                  <td>{d.bu}</td>
                  <td>{d.owner}</td>
                  <td>
                    <Chip tone={STATUS_TONE[d.jdStatus]}>{d.jdStatus}</Chip>
                  </td>
                  <td className="num">{d.matches || '—'}</td>
                  <td>{d.bestBand ? <BandChip band={d.bestBand} /> : '—'}</td>
                  <td className="row" style={{ flexWrap: 'nowrap' }}>
                    <button className="btn btn-sm" onClick={() => setOpen(d)}>
                      {needsAnalysis(d.jdStatus) ? 'Analyze JD' : 'Review JD'}
                    </button>
                    {d.matches > 0 && (
                      <Link className="btn btn-sm btn-tertiary" to={`/matches?req=${encodeURIComponent(d.codeRequisition)}`}>
                        View matches
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && <JdReview demand={open} onClose={() => setOpen(null)} />}
      {upload && <UploadDialog kind="dl" onClose={() => setUpload(false)} />}
    </div>
  )
}
