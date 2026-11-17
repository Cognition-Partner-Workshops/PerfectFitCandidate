import { createContext, useContext } from 'react'
import type { JobItem } from '../api/types'

export interface Task {
  id: string
  jobId?: number
  label: string
  status: JobItem['status']
  progress?: number
  link?: string | null
  reason?: string
  endedAt?: number
  hidden?: boolean
}

export interface Tasks {
  tasks: Task[]
  busy?: boolean
  hasFailed?: boolean
  trackTask: <T>(label: string, promise: Promise<T>, options?: { link?: string; failureReason?: (value: T) => string | undefined }) => Promise<T>
  addJob: (jobId: number, label?: string) => void
  dismiss: (id: string) => void
}

export const TaskContext = createContext<Tasks>({
  tasks: [], trackTask: (_label, promise) => promise, addJob: () => {}, dismiss: () => {},
})
export const useTasks = () => useContext(TaskContext)
export const isTaskActive = (task: Task) => task.status === 'queued' || task.status === 'running'

export function taskStatus(task: Task): string {
  switch (task.status) {
    case 'queued': return 'Started'
    case 'running': return `In Progress${task.progress !== undefined ? ` (${Math.round(Math.max(0, Math.min(1, task.progress)) * 100)}%)` : ''}`
    case 'completed': return 'Completed'
    case 'failed': return `Failed — ${task.reason ?? 'Please retry or contact an administrator.'}`
    case 'cancelled': return 'Cancelled'
  }
}

export function mergeJobs(tasks: Task[], jobs: JobItem[], dismissed: Set<string>, now: number): Task[] {
  const merged = new Map(tasks.map((task) => [task.id, task]))
  for (const job of jobs) {
    const id = `job-${job.jobId}`
    const previous = merged.get(id)
    const terminal = job.status !== 'queued' && job.status !== 'running'
    merged.set(id, {
      id, jobId: job.jobId, label: job.label, status: job.status, progress: job.progress,
      link: job.link, endedAt: terminal ? previous?.endedAt ?? now : undefined,
      hidden: dismissed.has(id),
    })
  }
  return [...merged.values()]
}
