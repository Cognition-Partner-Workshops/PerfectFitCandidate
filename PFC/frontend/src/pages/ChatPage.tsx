import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Send } from 'lucide-react'
import { api } from '../api'
import type { ChatAnswer, ChatEntry, ChatSource } from '../api/types'
import { useAuth } from '../auth/context'
import { Banner, Chip, ErrorState, Spinner } from '../components/ui'
import { formatDateTime } from '../lib/format'
import { useTasks } from '../lib/tasks'
import './ChatPage.css'

const EXAMPLES = [
  'How many open demands are there?',
  'What are the top five confirmed skills in demand?',
  'Who on the bench has Databricks and is C1–C2?',
  'Why is the candidate ranked #2 for T10001?',
  'Average match score by band for current results',
  'Top 5 candidates for T10001',
]

const active = (entry?: ChatEntry) => entry?.status === 'queued' || entry?.status === 'running'

function Sources({ sources }: { sources: ChatSource[] }) {
  if (!sources.length) return <p className="chat-muted">No source rows.</p>
  const columns = Array.from(new Set(sources.flatMap((source) => Object.keys(source.values))))
  return (
    <div className="table-wrap scroll">
      <table>
        <caption className="sr-only">Records used for this answer</caption>
        <thead><tr><th scope="col">Record</th>{columns.map((column) => <th key={column} scope="col">{column}</th>)}</tr></thead>
        <tbody>
          {sources.map((source) => (
            <tr key={`${source.kind}-${source.recordId}`}>
              <th scope="row"><Link to={source.link}>{source.label}</Link></th>
              {columns.map((column) => <td key={column}>{source.values[column] ?? '—'}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Answer({ answer, onRetry }: { answer: ChatAnswer; onRetry: () => void }) {
  return (
    <div className="chat-answer" aria-live="polite">
      <h2>Answer</h2>
      <p>{answer.text}</p>
      {answer.query && (
        <div className="chat-query">
          {answer.query.ranking && <p role="note">PFC ranking — human review required</p>}
          <div className="table-wrap scroll">
            <table className="data">
              <caption className="sr-only">Query results</caption>
              <thead><tr>{answer.query.columns.map((column, index) => <th key={index} scope="col">{column}</th>)}</tr></thead>
              <tbody>
                {answer.query.rows.map((row, index) => (
                  <tr key={index}>{row.map((cell, column) => <td key={column}>{cell === null ? '—' : String(cell)}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
          {answer.query.rows.length === 0 && <p className="chat-muted">No result rows.</p>}
          {answer.query.truncated && <p role="note">Results truncated to 500 rows.</p>}
          <details>
            <summary>Show SQL</summary>
            <p>{answer.query.explanation}</p>
            <pre><code>{answer.query.sql}</code></pre>
          </details>
        </div>
      )}
      {(answer.outcome === 'unavailable' || answer.outcome === 'uninterpretable') &&
        <button className="btn btn-sm" onClick={onRetry}>Edit and retry</button>}
    </div>
  )
}

export function ChatPage() {
  const { user } = useAuth()
  const { addJob } = useTasks()
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const jobId = Number(params.get('job'))
  const hasJob = Number.isSafeInteger(jobId) && jobId > 0
  const [question, setQuestion] = useState('')
  const input = useRef<HTMLTextAreaElement>(null)
  const historyKey = ['chat-history', user?.userId]
  const detailKey = ['chat-detail', user?.userId, jobId]
  const model = useQuery({
    queryKey: ['chat-status', user?.userId], queryFn: api.chatStatus, refetchInterval: 10_000,
  })
  const history = useQuery({
    queryKey: historyKey, queryFn: api.chatHistory,
    refetchInterval: (query) => query.state.data?.some(active) ? 1_500 : false,
  })
  const detail = useQuery({
    queryKey: detailKey, queryFn: () => api.chatDetail(jobId), enabled: hasJob,
    refetchInterval: (query) => active(query.state.data) ? 750 : false,
  })
  const ask = useMutation({
    mutationFn: api.askChat,
    onSuccess: (entry) => {
      qc.setQueryData(['chat-detail', user?.userId, entry.jobId], { ...entry, answer: null })
      void qc.invalidateQueries({ queryKey: historyKey })
      addJob(entry.jobId, 'Chat question')
      setQuestion('')
      setParams({ job: String(entry.jobId) })
    },
  })
  const stop = useMutation({
    mutationFn: api.cancelJob,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: historyKey })
      void qc.invalidateQueries({ queryKey: detailKey })
    },
  })
  const available = model.data?.available === true
  const running = active(detail.data)
  const editable = available && !ask.isPending
  const send = (event: FormEvent) => {
    event.preventDefault()
    if (editable && question.trim()) ask.mutate(question.trim())
  }
  const select = (entry?: ChatEntry) => {
    ask.reset()
    stop.reset()
    setQuestion('')
    setParams(entry ? { job: String(entry.jobId) } : {})
  }
  const prefill = (value: string) => {
    setQuestion(value)
    input.current?.focus()
  }

  return (
    <div className="page chat-page">
      <div className="page-header">
        <h1>Chat</h1>
        <Chip tone={available ? 'success' : 'neutral'}>{available ? 'Local model ready' : model.isPending ? 'Checking local model' : 'Local model unavailable'}</Chip>
      </div>
      <div className="chat-layout">
        <aside className="card chat-history" aria-label="Question history">
          <button className="btn" onClick={() => select()} disabled={ask.isPending}><Plus size={16} aria-hidden /> New question</button>
          <h2>Recent questions</h2>
          {history.isPending && <Spinner />}
          {history.isError && <ErrorState error={history.error} onRetry={() => void history.refetch()} />}
          {history.data?.length === 0 && <p className="chat-muted">No questions yet.</p>}
          {history.data?.map((entry) => (
            <button key={entry.jobId} className="chat-history-item" aria-current={entry.jobId === jobId ? 'true' : undefined}
              disabled={ask.isPending} onClick={() => select(entry)}>
              <span>{entry.question}</span>
              <small>{formatDateTime(entry.createdAt)}{active(entry) ? ' · In progress' : ''}</small>
            </button>
          ))}
        </aside>
        <section className="card chat-workspace" aria-label="Ask PFC">
          {model.isError
            ? <ErrorState error={model.error} onRetry={() => void model.refetch()} />
            : model.data?.available === false && (
              <Banner tone="warning">
                The local model is unavailable. Check the active model in <Link to="/settings">Settings</Link>.
                {' '}<button type="button" className="btn btn-sm" onClick={() => void model.refetch()}>Check again</button>
              </Banner>
            )}
          {hasJob && detail.isPending && <Spinner />}
          {hasJob && detail.isError && <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />}
          {hasJob && detail.data && (
            <>
              <div className="chat-question"><h2>Your question</h2><p>{detail.data.question}</p></div>
              {running && (
                <div className="row">
                  <p role="status">{stop.isSuccess ? 'Stopping…' : 'Reading PFC data locally…'}</p>
                  <button className="btn btn-sm" onClick={() => stop.mutate(jobId)} disabled={stop.isPending || stop.isSuccess}>Stop</button>
                </div>
              )}
              {detail.data.status === 'cancelled' && <p role="status">Question stopped.</p>}
              {detail.data.status === 'failed' && <Banner tone="danger">The question failed. <button className="btn btn-sm" onClick={() => prefill(detail.data!.question)}>Edit and retry</button></Banner>}
              {detail.data.answer && <Answer answer={detail.data.answer} onRetry={() => prefill(detail.data!.question)} />}
            </>
          )}
          {stop.isError && <ErrorState error={stop.error} />}
          {!hasJob && (
            <div className="chat-examples">
              <h2>Ask about demands, bench resources, or match results</h2>
              {EXAMPLES.map((example) => (
                <button key={example} className="btn" disabled={!editable} onClick={() => prefill(example)}>{example}</button>
              ))}
            </div>
          )}
          <form className="chat-form" onSubmit={send}>
            <label htmlFor="chat-question">Question</label>
            <textarea id="chat-question" ref={input} rows={3} maxLength={600} value={question} disabled={!editable}
              placeholder="Ask one question at a time…" onChange={(event) => setQuestion(event.target.value)} />
            <div className="row chat-form-actions">
              <p className="chat-muted">Use requisition codes or masked resource IDs. Keep personal information out of questions.</p>
              <button className="btn btn-primary" disabled={!editable || !question.trim()}>
                <Send size={16} aria-hidden /> {ask.isPending ? 'Sending…' : 'Ask'}
              </button>
            </div>
            {ask.isError && <ErrorState error={ask.error} />}
          </form>
        </section>
        {hasJob && detail.data?.answer && (
          <section className="card chat-sources" aria-labelledby="chat-sources-heading">
            <h2 id="chat-sources-heading">Sources ({detail.data.answer.sources.length})</h2>
            <Sources sources={detail.data.answer.sources} />
          </section>
        )}
      </div>
    </div>
  )
}
