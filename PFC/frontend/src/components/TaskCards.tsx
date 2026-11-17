import { useState } from 'react'
import { CheckCircle2, CircleX, Clock, LoaderCircle, Minus, X, ListChecks } from 'lucide-react'
import { taskStatus, useTasks } from '../lib/tasks'

export function TaskCards() {
  const { tasks, dismiss } = useTasks()
  const [collapsed, setCollapsed] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const ordered = [...tasks].reverse()
  return (
    <section className="task-popups" aria-label="Task status" aria-live="polite" aria-relevant="additions text">
      {tasks.length > 0 && (collapsed ? (
        <button className="btn task-minimized" aria-label={`Show tasks (${tasks.length})`} onClick={() => setCollapsed(false)}>
          <ListChecks size={18} aria-hidden /> {tasks.length}
        </button>
      ) : (
        <>
          <div className="task-toolbar">
            {tasks.length > 3 && <button className="btn btn-sm" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? 'Show fewer' : `+${tasks.length - 3} more`}</button>}
            <button className="icon-btn" aria-label="Minimize tasks" onClick={() => setCollapsed(true)}><Minus size={16} /></button>
          </div>
          <div className="task-stack">
            {(expanded ? ordered : ordered.slice(0, 3)).map((task) => {
              const Icon = task.status === 'completed' ? CheckCircle2 : task.status === 'failed' || task.status === 'cancelled' ? CircleX : task.status === 'running' ? LoaderCircle : Clock
              const text = `${task.label}: ${taskStatus(task)}`
              return (
                <div key={task.id} className={`task-card task-${task.status}`} onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); dismiss(task.id) } }}>
                  <Icon size={18} aria-hidden className="task-status-icon" />
                  {task.link ? <a className="task-text" href={task.link} title={text}>{text}</a> : <span className="task-text" title={text}>{text}</span>}
                  <button className="icon-btn" aria-label={`Dismiss ${task.label}`} onClick={() => dismiss(task.id)}><X size={16} /></button>
                </div>
              )
            })}
          </div>
        </>
      ))}
    </section>
  )
}
