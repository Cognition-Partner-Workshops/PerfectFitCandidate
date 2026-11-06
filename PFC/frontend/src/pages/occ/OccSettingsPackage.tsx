import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import { Banner, Chip, ErrorState } from '../../components/ui'
import { downloadBase64 } from '../../lib/download'
import { formatDateTime } from '../../lib/format'
import type { ExportedPackage } from '../../api/types'

/** Export a signed, versioned settings package (REQ-OCC-01, GUI_OCC_DESIGN.md §16.15). */
export function OccSettingsPackage() {
  const qc = useQueryClient()
  const last = useQuery({ queryKey: ['occ', 'package'], queryFn: () => api.lastExportedPackage() })
  // The passphrase lives only in this field's state and is cleared after each attempt; never persisted or logged.
  const [passphrase, setPassphrase] = useState('')
  const [otps, setOtps] = useState<ExportedPackage['oneTimePasswords']>([])
  const exp = useMutation({
    mutationFn: (pass: string) => api.exportPackage(pass),
    onSettled: () => setPassphrase(''),
    onSuccess: ({ contentBase64, oneTimePasswords, ...info }) => {
      downloadBase64(contentBase64, info.fileName)
      setOtps(oneTimePasswords)
      qc.setQueryData(['occ', 'package'], info)
    },
  })
  const p = last.data
  return (
    <section className="card stack" aria-label="Settings packages">
      <div className="row">
        <h2>Settings packages</h2>
        <span className="spacer" />
      </div>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault()
          exp.mutate(passphrase)
        }}
      >
        <label className="field">
          Signing-key passphrase
          <input type="password" autoComplete="off" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} />
        </label>
        <button type="submit" className="btn btn-primary" disabled={exp.isPending || !passphrase}>
          Export settings package
        </button>
      </form>
      <p className="small muted">
        The package contains configuration, approvals and Analyst accounts (one-time password hash, change required) — never secrets or tokens. Share it via the synced
        folder; if that fails, send it by Teams or email. Analysts import it in Settings → Settings package.
      </p>
      {exp.isError && <ErrorState error={exp.error} />}
      {p ? (
        <div className="stack">
          {exp.isSuccess && <Banner tone="success">Created {p.fileName}.</Banner>}
          {otps.length > 0 && (
            <div className="stack" aria-label="One-time passwords">
              <Banner tone="warning">Hand these one-time passwords to each Analyst now; they are not shown again.</Banner>
              <table className="data">
                <thead>
                  <tr>
                    <th>Analyst</th>
                    <th>One-time password</th>
                  </tr>
                </thead>
                <tbody>
                  {otps.map((o) => (
                    <tr key={o.email}>
                      <td>{o.email}</td>
                      <td className="mono">{o.otp}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
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
