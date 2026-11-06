import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api'
import { KpiTile } from '../../components/ui'

export function OccOverview() {
  const users = useQuery({ queryKey: ['occ', 'users'], queryFn: () => api.listUsers() })
  const synonyms = useQuery({ queryKey: ['occ', 'synonyms'], queryFn: () => api.listSynonyms() })
  const templates = useQuery({ queryKey: ['occ', 'templates'], queryFn: () => api.listTemplates() })
  const map = useQuery({ queryKey: ['occ', 'status-map'], queryFn: () => api.getStatusMap() })
  const health = useQuery({ queryKey: ['health'], queryFn: () => api.getHealth() })
  const active = templates.data?.find((t) => t.status === 'active')
  return (
    <div className="stack">
      <div className="kpi-row">
        <KpiTile label="Users" value={users.data?.filter((u) => u.status === 'Active').length ?? '—'} hint={`${users.data?.filter((u) => u.status === 'Locked').length ?? 0} locked`} />
        <KpiTile label="Synonyms" value={synonyms.data?.length ?? '—'} hint={`${synonyms.data?.filter((s) => s.status === 'Suggested').length ?? 0} new suggestions`} />
        <KpiTile label="Scoring template" value={active ? `v${active.version}` : '—'} hint="active" accent="var(--color-chart-3)" />
        <KpiTile label="Unmapped statuses" value={map.data?.unmapped.length ?? '—'} hint={<Link to="/occ/status-map">Review mapping</Link>} accent="var(--color-warning)" />
        <KpiTile label="Data volume encryption" value={health.data?.dataVolumeEncryption ?? '—'} hint="BitLocker, informational" />
      </div>
    </div>
  )
}
