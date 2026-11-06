import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { PackagePreview } from '../../api/types'
import { Banner, Chip, ErrorState, Modal } from '../../components/ui'
import { formatDateTime } from '../../lib/format'

/**
 * Settings-package import with signature/version validation, change preview, atomic apply and
 * one-click rollback (REQ-OCC-02, TDD.md §13.2, GUI_OCC_DESIGN.md §15).
 */
export function SettingsPackagePanel() {
  const qc = useQueryClient()
  const history = useQuery({ queryKey: ['package-history'], queryFn: () => api.packageHistory() })
  const [preview, setPreview] = useState<PackagePreview | null>(null)
  const [confirmRollback, setConfirmRollback] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const doPreview = useMutation({ mutationFn: (f: File) => api.previewPackage(f), onSuccess: (p) => { setPreview(p); setNotice(null) } })
  const apply = useMutation({
    mutationFn: (id: string) => api.importPackage(id),
    onSuccess: (h) => {
      qc.setQueryData(['package-history'], h)
      setNotice(`Settings package v${preview?.packageVersion} applied. Imported Analyst accounts must change their one-time password at first sign-in.`)
      setPreview(null)
    },
  })
  const rollback = useMutation({
    mutationFn: () => api.rollbackPackage(),
    onSuccess: (h) => {
      qc.setQueryData(['package-history'], h)
      setConfirmRollback(false)
      setNotice(`Rolled back to v${h[0]?.version}.`)
    },
  })
  const installed = history.data?.[0]
  const valid = preview && preview.errors.length === 0
  return (
    <section className="card stack" aria-label="Settings package">
      <h2>Settings package</h2>
      <p className="small muted">
        Import the signed <span className="mono">.pfcpkg</span> shared by your OCC Admin (synced folder, Teams or email). Packages placed in{' '}
        <span className="mono">inbox\</span> are detected at startup. Trust comes from the signature, not the channel.
      </p>
      <div className="row">
        Installed: {installed ? <strong>v{installed.version}</strong> : 'none'}
        <span className="spacer" />
        <label className="btn">
          Choose package…
          <input
            type="file"
            accept=".pfcpkg"
            hidden
            aria-label="Settings package file"
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) doPreview.mutate(f)
              e.target.value = ''
            }}
          />
        </label>
      </div>
      {notice && <Banner tone="success">{notice}</Banner>}
      {doPreview.isError && <ErrorState error={doPreview.error} />}
      {preview && (
        <div className="stack" aria-label="Package preview">
          <h3>
            Preview — {preview.fileName} (v{preview.packageVersion}, installed v{preview.installedVersion})
          </h3>
          <div className="row">
            <Chip tone={preview.signatureValid ? 'success' : 'danger'}>{preview.signatureValid ? '✓ Signature valid' : '✕ Signature invalid'}</Chip>
            <Chip tone={preview.schemaCompatible ? 'success' : 'danger'}>{preview.schemaCompatible ? '✓ Schema compatible' : '✕ Schema incompatible'}</Chip>
            <Chip tone={preview.appCompatible ? 'success' : 'danger'}>{preview.appCompatible ? '✓ App compatible' : '✕ App incompatible'}</Chip>
            <Chip tone={preview.newer ? 'success' : 'danger'}>{preview.newer ? '✓ Newer version' : '✕ Not newer'}</Chip>
          </div>
          {preview.errors.length > 0 && (
            <Banner tone="danger">
              This package cannot be applied. Current settings are unchanged.
              <ul>{preview.errors.map((e) => <li key={e}>{e}</li>)}</ul>
            </Banner>
          )}
          <div className="table-wrap">
            <table className="data" aria-label="Package changes">
              <thead>
                <tr>
                  <th>Area</th>
                  <th>Change</th>
                  <th>Item</th>
                  <th>Before → After</th>
                </tr>
              </thead>
              <tbody>
                {preview.changes.map((c, i) => (
                  <tr key={i}>
                    <td>{c.area}</td>
                    <td>
                      <Chip tone={c.change === 'removed' ? 'danger' : c.change === 'added' ? 'success' : 'info'}>{c.change}</Chip>
                    </td>
                    <td>{c.item}</td>
                    <td>
                      {c.before !== undefined && <span className="diff-before">{c.before}</span>} {c.after !== undefined && <span className="diff-after">{c.after}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="modal-actions">
            <button className="btn" onClick={() => setPreview(null)}>
              Cancel
            </button>
            <button className="btn btn-primary" disabled={!valid || apply.isPending} onClick={() => apply.mutate(preview.previewId)}>
              Apply package
            </button>
          </div>
          {apply.isError && <ErrorState error={apply.error} />}
        </div>
      )}
      <h3>Import history</h3>
      <table className="data" aria-label="Import history">
        <thead>
          <tr>
            <th>Version</th>
            <th>File</th>
            <th>Imported</th>
            <th>By</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(history.data ?? []).map((h) => (
            <tr key={`${h.version}-${h.importedAt}`}>
              <td>v{h.version}</td>
              <td className="mono small">{h.fileName}</td>
              <td>{formatDateTime(h.importedAt)}</td>
              <td>{h.importedBy}</td>
              <td>
                {h.canRollback && (
                  <button className="btn btn-sm" onClick={() => setConfirmRollback(true)}>
                    Roll back
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {confirmRollback && (
        <Modal
          title="Roll back settings package?"
          onClose={() => setConfirmRollback(false)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirmRollback(false)}>
                Cancel
              </button>
              <button className="btn btn-danger" onClick={() => rollback.mutate()}>
                Roll back to v{history.data?.[1]?.version}
              </button>
            </>
          }
        >
          <p>The previous package is restored atomically. This action is recorded in the audit log.</p>
          {rollback.isError && <ErrorState error={rollback.error} />}
        </Modal>
      )}
    </section>
  )
}
