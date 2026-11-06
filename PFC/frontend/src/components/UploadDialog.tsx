import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import type { UploadPreview } from '../api/types'
import { Banner, ErrorState, Modal } from './ui'

/** Upload → preview/validation → Commit (REQ-ING-01..03/06, GUI_OCC_DESIGN.md §9.1, §10.1). */
export function UploadDialog({ kind, onClose }: { kind: 'dl' | 'ct'; onClose: () => void }) {
  const qc = useQueryClient()
  const [preview, setPreview] = useState<UploadPreview | null>(null)
  const label = kind === 'dl' ? 'Demand List' : 'Candidate Tracker'
  const upload = useMutation({ mutationFn: (f: File) => api.uploadWorkbook(kind, f), onMutate: () => setPreview(null), onSuccess: setPreview })
  const commit = useMutation({
    mutationFn: (id: number) => api.commitIngestion(id),
    onSuccess: () => {
      void qc.invalidateQueries()
      onClose()
    },
  })
  const discard = () => {
    if (preview) void api.discardIngestion(preview.ingestionRunId)
    onClose()
  }
  return (
    <Modal
      title={`Upload ${label} — preview`}
      onClose={discard}
      actions={
        <>
          <button className="btn" onClick={discard}>
            Cancel
          </button>
          {preview && !preview.alreadyImported && (
            <button className="btn btn-primary" disabled={commit.isPending} onClick={() => commit.mutate(preview.ingestionRunId)}>
              Commit {preview.rowsRead - preview.rejected} rows
            </button>
          )}
        </>
      }
    >
      <label className="field">
        Workbook (.xlsx)
        <input
          type="file"
          accept=".xlsx"
          aria-label={`${label} workbook`}
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) upload.mutate(f)
          }}
        />
      </label>
      {upload.isPending && <span className="muted">Reading workbook…</span>}
      {upload.isError && <ErrorState error={upload.error} />}
      {preview && (
        <div className="stack">
          {preview.alreadyImported && <Banner tone="info">This file was already imported — nothing to do.</Banner>}
          <span className="small">
            File: {preview.fileName} · Sheet detected: “{preview.sheet}” (by header)
          </span>
          <div className="row num">
            <span>Rows read {preview.rowsRead}</span> │ <span>New {preview.new}</span> │ <span>Updated {preview.updated}</span> │{' '}
            <span>Unchanged {preview.unchanged}</span> │ <span>Rejected {preview.rejected}</span>
          </div>
          {preview.warnings.length > 0 && (
            <Banner tone="warning">
              <strong>Warnings ({preview.warnings.length})</strong>
              <ul>{preview.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </Banner>
          )}
          <span className="small muted">Committed changes apply to matching immediately and are queued for OCC review.</span>
        </div>
      )}
      {commit.isError && <ErrorState error={commit.error} />}
    </Modal>
  )
}
