import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import { Banner, Chip, ErrorState } from '../../components/ui'
import { formatDateTime } from '../../lib/format'

/** Export a signed, versioned settings package (REQ-OCC-01, GUI_OCC_DESIGN.md §16.15). */
export function OccSettingsPackage() {
  const qc = useQueryClient()
  const last = useQuery({ queryKey: ['occ', 'package'], queryFn: () => api.lastExportedPackage() })
  const exp = useMutation({ mutationFn: () => api.exportPackage(), onSuccess: (p) => qc.setQueryData(['occ', 'package'], p) })
  const p = last.data
  return (
    <section className="card stack" aria-label="Settings packages">
      <div className="row">
        <h2>Settings packages</h2>
        <span className="spacer" />
        <button className="btn btn-primary" onClick={() => exp.mutate()} disabled={exp.isPending}>
          Export settings package
        </button>
      </div>
      <p className="small muted">
        The package contains configuration, approvals and Analyst accounts (one-time password hash, change required) — never secrets or tokens. Share it via the synced
        folder; if that fails, send it by Teams or email. Analysts import it in Settings → Settings package.
      </p>
      {exp.isError && <ErrorState error={exp.error} />}
      {p ? (
        <div className="stack">
          {exp.isSuccess && <Banner tone="success">Created {p.fileName}.</Banner>}
          <div className="row">
            <strong className="mono">{p.fileName}</strong>
            <Chip tone={p.signed ? 'success' : 'danger'}>{p.signed ? '✓ Signed (Ed25519)' : 'Unsigned'}</Chip>
            <span className="muted small">Version {p.version} · {formatDateTime(p.createdAt)}</span>
          </div>
          <table className="data" aria-label="Package contents">
            <thead>
              <tr>
                <th>Contents</th>
                <th>Items</th>
              </tr>
            </thead>
            <tbody>
              {p.contents.map((c) => (
                <tr key={c.label}>
                  <td>{c.label}</td>
                  <td className="num">{c.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">No package exported from this instance yet.</p>
      )}
    </section>
  )
}
