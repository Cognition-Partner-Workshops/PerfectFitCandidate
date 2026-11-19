import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api'
import type { LlmModel } from '../../api/types'
import { Banner, Chip, ErrorState, Spinner } from '../../components/ui'

/** Approved local models: Analysts may only pick approved ones (REQ-LLM-01, GUI_OCC_DESIGN.md §16.10). */
export function OccModels() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['occ', 'models'], queryFn: () => api.listOccModels() })
  const settings = useQuery({ queryKey: ['maintenance-settings'], queryFn: () => api.maintenance.settings() })
  const [context, setContext] = useState<number | null>(null)
  const save = useMutation({
    mutationFn: (value: number) => api.maintenance.saveSettings({ 'llm.ctx_size': value }),
    onSuccess: (values) => {
      qc.setQueryData(['maintenance-settings'], values)
      setContext(null)
      void qc.invalidateQueries({ queryKey: ['llm-health'] })
    },
  })
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
              <td>{m.name}{m.thinking && <><br /><Chip>Thinking</Chip><p className="meta">slower: reasons before answering (~4× wall time on JD parsing)</p></>}</td>
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
      <section className="stack" aria-label="Local model context">
        <h3>Context size</h3>
        <p>Saving restarts the local model. Extra RAM estimates are relative to 4096 tokens and use the default generation model.</p>
        <p className="meta">f16 KV cache: layers × KV heads × head dim × 2 × 2 bytes per token</p>
        {settings.isLoading ? <Spinner /> : settings.isError ? <ErrorState error={settings.error} /> : <>
          <label className="field">Context size (tokens)
            <select value={context ?? Number(settings.data?.['llm.ctx_size'] ?? 4096)} onChange={(event) => { setContext(Number(event.target.value)); save.reset() }}>
              {[4096, 8192, 16384].map((size) => {
                const kv = models.find((model) => model.isDefault && model.purpose === 'generation')?.kvBytesPerToken
                const mib = kv == null ? null : (size - 4096) * kv / 1024 ** 2
                return <option key={size} value={size}>{size}{size === 4096 ? ' (default)' : ''} · extra RAM: {mib == null ? 'unknown' : mib >= 1024 ? `${(mib / 1024).toFixed(2)} GiB (${mib} MiB)` : `${mib} MiB`}</option>
              })}
            </select>
          </label>
          <button className="btn btn-primary" disabled={context === null || save.isPending} onClick={() => context !== null && save.mutate(context)}>Save context size</button>
        </>}
        {save.isError && <Banner tone="danger">{(save.error as Error).message}</Banner>}
        {save.isSuccess && <Banner tone="success">Context size saved. Local model restart requested.</Banner>}
      </section>
    </section>
  )
}
