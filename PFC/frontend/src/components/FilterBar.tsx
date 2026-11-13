import type { Band, ResultFilters } from '../api/types'
import { BAND_ORDER, bandLabel } from '../lib/bands'
import { MultiSelect } from './MultiSelect'
import { activeFilterCount } from '../lib/filters'

export interface FilterOptions {
  requisitions: string[]
  accounts: string[]
  bus: string[]
  grades: string[]
  locations: string[]
  regions: string[]
  statuses: string[]
}

interface Props {
  value: ResultFilters
  onChange: (next: ResultFilters) => void
  options: FilterOptions
  /** Dashboard uses a single Grade filter; Match Results splits Demand Grade / Person Grade. */
  splitGrade?: boolean
}

/**
 * Shared filter bar (REQ-OUT-08, REQ-MAT-10): Code Requisition, Account Name, BU, Grade, Location,
 * region, band, status, candidate type, incomplete identity, mandatory-skill status and score range.
 */
export function FilterBar({ value, onChange, options, splitGrade = false }: Props) {
  const set = <K extends keyof ResultFilters>(k: K, v: ResultFilters[K]) => onChange({ ...value, [k]: v })
  const num = (v: string) => (v === '' ? undefined : Math.max(0, Math.min(100, Number(v))))
  return (
    <div className="filter-bar" role="group" aria-label="Filters">
      <MultiSelect label="Code Requisition" value={value.codeRequisition} options={options.requisitions} onChange={(v) => set('codeRequisition', v)} />
      <MultiSelect label="Account Name" value={value.accountName} options={options.accounts} onChange={(v) => set('accountName', v)} />
      <MultiSelect label="BU" value={value.bu} options={options.bus} onChange={(v) => set('bu', v)} />
      {splitGrade ? (
        <>
          <MultiSelect label="Demand Grade" value={value.demandGrade} options={options.grades} onChange={(v) => set('demandGrade', v)} />
          <MultiSelect label="Person Grade" value={value.personGrade} options={options.grades} onChange={(v) => set('personGrade', v)} />
        </>
      ) : (
        <MultiSelect label="Grade" value={value.demandGrade} options={options.grades} onChange={(v) => set('demandGrade', v)} />
      )}
      <MultiSelect label="Location" value={value.location} options={options.locations} onChange={(v) => set('location', v)} />
      <MultiSelect label="Region" value={value.region} options={options.regions} onChange={(v) => set('region', v)} />
      <MultiSelect
        label="Band"
        value={value.band}
        options={BAND_ORDER}
        render={(b) => bandLabel(b as Band)}
        onChange={(v) => set('band', v)}
      />
      <MultiSelect label="Status" value={value.status} options={options.statuses} onChange={(v) => set('status', v)} />
      <MultiSelect
        label="Candidate type"
        value={value.candidateType}
        options={['Internal', 'External', 'Unknown']}
        onChange={(v) => set('candidateType', v)}
      />
      <MultiSelect
        label="Incomplete identity"
        value={value.incompleteIdentity}
        options={['yes', 'no']}
        render={(o) => (o === 'yes' ? 'Incomplete only' : 'Complete only')}
        onChange={(v) => set('incompleteIdentity', v)}
      />
      <MultiSelect
        label="Mandatory skills"
        value={value.mandatorySkills}
        options={['met', 'gap']}
        render={(o) => (o === 'met' ? 'All met' : 'Has mandatory gap')}
        onChange={(v) => set('mandatorySkills', v)}
      />
      <label>
        Score min
        <input type="number" min={0} max={100} value={value.scoreMin ?? ''} onChange={(e) => set('scoreMin', num(e.target.value))} aria-label="Score min" />
      </label>
      <label>
        Score max
        <input type="number" min={0} max={100} value={value.scoreMax ?? ''} onChange={(e) => set('scoreMax', num(e.target.value))} aria-label="Score max" />
      </label>
      {activeFilterCount(value) > 0 && (
        <button className="btn btn-tertiary btn-sm" onClick={() => onChange({})}>
          Clear all ({activeFilterCount(value)})
        </button>
      )}
    </div>
  )
}
