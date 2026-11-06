import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { LlmModel } from '../../api/types'
import { Banner, Chip, ErrorState, Spinner } from '../../components/ui'

/** Approved local models: Analysts may only pick approved ones (REQ-LLM-01, GUI_OCC_DESIGN.md §16.10). */
export function OccModels() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['occ', 'models'], queryFn: () => api.listOccModels() })
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Partial<Pick<LlmModel, 'approved' | 'isDefault'>> }) => api.updateOccModel(id, patch),
    onSuccess: (m) => {
      qc.setQueryData(['occ', 'models'], m)
      void qc.invalidateQueries({ queryKey: ['models'] })
    },
  })
  if (q.isLoading) return <Spinner />
  if (q.isError) return <ErrorState error={q.error} />
  const models = q.data ?? []
  return (
    <section className="card stack" aria-label="Approved models">
      <h2>Approved local models</h2>
      <p className="meta">Local GGUF/ONNX models only in Phase 1. One default per purpose; the default cannot be revoked.</p>
      {update.isError && <Banner tone="danger">{(update.error as Error).message}</Banner>}
      <table className="data">
        <thead>
          <tr>
            <th>Model</th>
            <th>Purpose</th>
            <th>RAM / disk</th>
            <th>Licence</th>
            <th>Approved</th>
            <th>Default</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => (
            <tr key={m.id}>
              <td>{m.name}</td>
              <td>{m.purpose === 'generation' ? 'Generation' : 'Embedding'}</td>
              <td>
                {m.ramGb} GB / {m.diskGb} GB
              </td>
              <td>{m.licence}</td>
              <td>
                <input
                  type="checkbox"
                  aria-label={`Approved ${m.name}`}
                  checked={m.approved}
                  disabled={m.isDefault}
                  onChange={() => update.mutate({ id: m.id, patch: { approved: !m.approved } })}
                />
              </td>
              <td>
                {m.isDefault ? (
                  <Chip tone="success">Default</Chip>
                ) : (
                  <button className="btn btn-sm" disabled={!m.approved} aria-label={`Make ${m.name} default`} onClick={() => update.mutate({ id: m.id, patch: { isDefault: true } })}>
                    Make default
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
