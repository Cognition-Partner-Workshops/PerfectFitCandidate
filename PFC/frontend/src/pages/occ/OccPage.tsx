import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { OccOverview } from './OccOverview'
import { OccUsers } from './OccUsers'
import { OccStatusMap } from './OccStatusMap'
import { OccSynonyms } from './OccSynonyms'
import { OccScoringTemplate } from './OccScoringTemplate'
import { OccResourceTeam } from './OccResourceTeam'
import { OccInternalDomains } from './OccInternalDomains'
import { OccSettingsPackage } from './OccSettingsPackage'

const GROUPS: [string, [string, string][]][] = [
  ['People', [['users', 'Users & passwords'], ['resource-team', 'Resource-team list']]],
  ['Data', [['status-map', 'Status list & mapping'], ['synonyms', 'Skill synonyms'], ['internal-domains', 'Internal domains']]],
  ['Matching', [['scoring-template', 'Scoring template']]],
  ['Operations', [['settings-package', 'Settings packages']]],
]

/** OCC — Object Control Center, Admin only (REQ-ROLE-03, GUI_OCC_DESIGN.md §16). */
export function OccPage() {
  return (
    <div className="page">
      <div className="page-header">
        <h1>OCC — Object Control Center</h1>
        <span className="meta">Admin only · every change is audit-logged</span>
      </div>
      <div className="subnav-layout">
        <nav className="subnav" aria-label="OCC sections">
          <NavLink to="/occ/overview">Overview</NavLink>
          {GROUPS.map(([g, items]) => (
            <div key={g} className="stack" style={{ gap: 2 }}>
              <div className="group">{g}</div>
              {items.map(([id, label]) => (
                <NavLink key={id} to={`/occ/${id}`}>
                  {label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <Routes>
          <Route index element={<Navigate to="overview" replace />} />
          <Route path="overview" element={<OccOverview />} />
          <Route path="users" element={<OccUsers />} />
          <Route path="resource-team" element={<OccResourceTeam />} />
          <Route path="status-map" element={<OccStatusMap />} />
          <Route path="synonyms" element={<OccSynonyms />} />
          <Route path="internal-domains" element={<OccInternalDomains />} />
          <Route path="scoring-template" element={<OccScoringTemplate />} />
          <Route path="settings-package" element={<OccSettingsPackage />} />
          <Route path="*" element={<Navigate to="overview" replace />} />
        </Routes>
      </div>
    </div>
  )
}
