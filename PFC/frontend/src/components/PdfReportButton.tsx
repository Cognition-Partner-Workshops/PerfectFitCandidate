import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { PdfRequest, PdfSection } from '../api/reports'
import type { DashboardFilters } from '../api/types'
import type { ResultsQuery } from '../api/reports'
import { useTasks } from '../lib/tasks'
import { ErrorState, Modal } from './ui'

const dashboardSections: [PdfSection, string][] = [
  ['cover', 'Cover and provenance'], ['summary', 'Summary and KPIs'], ['demand', 'Demand analytics'],
  ['bench', 'Bench analytics'], ['match', 'Match & communication'], ['unmatched', 'Unmatched demands'],
  ['extended-other', 'Further analytics'], ['data-quality-method', 'Data quality & scoring method'],
]
const resultsSections: [PdfSection, string][] = [['cover', 'Cover and provenance'], ['match-results', 'Match results']]

export function PdfReportButton(props: { report: PdfRequest['report']; filters?: DashboardFilters; resultFilters?: ResultsQuery }) {
  const [open, setOpen] = useState(false)
  return <><button className="btn" onClick={() => setOpen(true)}>PDF report</button>{open && <PdfDialog {...props} onClose={() => setOpen(false)} />}</>
}

function PdfDialog({ report, filters, resultFilters, onClose }: { report: PdfRequest['report']; filters?: DashboardFilters; resultFilters?: ResultsQuery; onClose: () => void }) {
  const options = report === 'dashboard' ? dashboardSections : resultsSections
  const [scope, setScope] = useState<PdfRequest['scope']>('filters')
  const [orientation, setOrientation] = useState<PdfRequest['orientation']>(report === 'dashboard' ? 'portrait' : 'landscape')
  const [sections, setSections] = useState<PdfSection[]>(options.map(([id]) => id))
  const qc = useQueryClient()
  const { addJob } = useTasks()
  const valid = sections.length > 0 && (report === 'dashboard' || sections.length === 2)
  const generate = useMutation({
    mutationFn: () => api.generatePdf({ report, scope, sections: options.map(([id]) => id).filter((id) => sections.includes(id)), orientation, ...(scope === 'filters' ? report === 'dashboard' ? { filters } : { resultFilters } : {}) }),
    onSuccess: ({ jobId }) => {
      addJob(jobId, 'PDF report')
      void qc.invalidateQueries({ queryKey: ['jobs'] })
      onClose()
    },
  })
  return <Modal title="PDF report" onClose={onClose} actions={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={!valid || generate.isPending} onClick={() => generate.mutate()}>{generate.isPending ? 'Generating…' : 'Generate'}</button></>}>
    <p>Emails are always masked. The report is generated locally in the background — find it in the task cards (bottom right).</p>
    <label className="field">Scope<select value={scope} onChange={(e) => setScope(e.target.value as PdfRequest['scope'])}><option value="filters">Current filters</option><option value="all">All</option></select></label>
    <fieldset><legend>Sections</legend>{options.map(([id, label]) => <label key={id} className="row"><input type="checkbox" checked={sections.includes(id)} onChange={(e) => setSections((old) => e.target.checked ? [...old, id] : old.filter((s) => s !== id))} />{label}</label>)}</fieldset>
    {!valid && <p role="alert">{report === 'dashboard' ? 'Select at least one section.' : 'Cover and match results are both required.'}</p>}
    <label className="field">Orientation<select value={orientation} onChange={(e) => setOrientation(e.target.value as PdfRequest['orientation'])}><option value="portrait">Portrait</option><option value="landscape">Landscape</option></select></label>
    {generate.isError && <ErrorState error={generate.error} />}
  </Modal>
}
