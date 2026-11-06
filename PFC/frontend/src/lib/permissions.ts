import type { Role } from '../api/types'
import { featureFlags } from '../config'

/** Access levels from the role-visibility matrix (GUI_OCC_DESIGN.md §5). */
export type Access = 'F' | 'R' | 'P' | '-'

export type Capability =
  | 'dashboard'
  | 'demands'
  | 'bench'
  | 'excludedView'
  | 'matchResults'
  | 'matchOverrides'
  | 'candidateProfile'
  | 'emailApproval'
  | 'chat'
  | 'settings'
  | 'settingsChooseModel'
  | 'settingsPackageImport'
  | 'scoringTemplateVersion'
  | 'occ'
  | 'auditLog'

export const ROLE_MATRIX: Record<Capability, Record<Role, Access>> = {
  dashboard: { Analyst: 'F', Admin: 'F' },
  demands: { Analyst: 'F', Admin: 'F' },
  bench: { Analyst: 'F', Admin: 'F' },
  excludedView: { Analyst: 'R', Admin: 'R' },
  matchResults: { Analyst: 'F', Admin: 'F' },
  matchOverrides: { Analyst: 'F', Admin: 'F' },
  candidateProfile: { Analyst: 'F', Admin: 'F' },
  emailApproval: { Analyst: 'F', Admin: 'F' },
  chat: { Analyst: 'F', Admin: 'F' },
  settings: { Analyst: 'F', Admin: 'F' },
  settingsChooseModel: { Analyst: 'P', Admin: 'F' },
  settingsPackageImport: { Analyst: 'F', Admin: 'F' },
  scoringTemplateVersion: { Analyst: 'R', Admin: 'F' },
  occ: { Analyst: '-', Admin: 'F' },
  auditLog: { Analyst: '-', Admin: 'F' },
}

export function access(role: Role, capability: Capability): Access {
  return ROLE_MATRIX[capability][role]
}

export function canView(role: Role, capability: Capability): boolean {
  return access(role, capability) !== '-'
}

export function canEdit(role: Role, capability: Capability): boolean {
  return access(role, capability) === 'F'
}

export interface NavItem {
  id: string
  label: string
  path: string
  capability: Capability
  phase: 1 | 2 | 3
  adminSection?: boolean
}

/** Left-panel tabs in fixed order (GUI_OCC_DESIGN.md §4.1). */
export const NAV_ITEMS: NavItem[] = [
  { id: 'dashboard', label: 'Dashboard', path: '/dashboard', capability: 'dashboard', phase: 1 },
  { id: 'demands', label: 'Demand List', path: '/demands', capability: 'demands', phase: 1 },
  { id: 'bench', label: 'Bench List', path: '/bench', capability: 'bench', phase: 1 },
  { id: 'matches', label: 'Match Results', path: '/matches', capability: 'matchResults', phase: 1 },
  { id: 'candidates', label: 'Candidate Profile', path: '/candidates', capability: 'candidateProfile', phase: 1 },
  { id: 'email', label: 'Email Approval', path: '/email', capability: 'emailApproval', phase: 1 },
  { id: 'chat', label: 'Chat', path: '/chat', capability: 'chat', phase: 2 },
  { id: 'settings', label: 'Settings', path: '/settings', capability: 'settings', phase: 1 },
  { id: 'occ', label: 'OCC', path: '/occ', capability: 'occ', phase: 1, adminSection: true },
]

function phaseEnabled(item: NavItem): boolean {
  if (item.id === 'chat') return featureFlags.chat
  return item.phase === 1
}

/** Tabs a role sees: OCC is not rendered for Analysts (REQ-ROLE-03); Chat is hidden by flag in Phase 1. */
export function visibleNav(role: Role): NavItem[] {
  return NAV_ITEMS.filter((item) => canView(role, item.capability) && phaseEnabled(item))
}
