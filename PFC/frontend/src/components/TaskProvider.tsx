import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api'
import { ApiError } from '../api/client'
import type { ApiClient } from '../api/client'
import { isTaskActive, mergeJobs, TaskContext } from '../lib/tasks'
import type { Task, Tasks } from '../lib/tasks'

function safeReason(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) return 'Sign in or check your permissions.'
    if (error.status === 422) return 'Check the input and retry.'
  }
  return 'Please retry or contact an administrator.'
}

export function TaskProvider({ children, client = api }: { children: ReactNode; client?: Pick<ApiClient, 'listJobs'> }) {
  const [tasks, setTasks] = useState<Task[]>([])
  const dismissed = useRef(new Set<string>())
  const sequence = useRef(0)
  const busy = tasks.some(isTaskActive)
  const hasFailed = tasks.some((task) => task.status === 'failed' && (task.endedAt ?? 0) > Date.now() - 15 * 60_000)
  const dismiss = useCallback((id: string) => {
    dismissed.current.add(id)
    setTasks((previous) => previous.map((task) => task.id === id ? { ...task, hidden: true } : task))
  }, [])

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let inFlight = false
    const poll = async () => {
      if (stopped || document.hidden || inFlight) return
      inFlight = true
      try {
        const jobs = await client.listJobs()
        if (!stopped) setTasks((previous) => mergeJobs(previous, jobs, dismissed.current, Date.now()))
      } catch {
        // Poll failures do not erase tasks or surface raw service errors.
      } finally {
        inFlight = false
        if (!stopped && !document.hidden) timer = setTimeout(() => void poll(), busy ? 2000 : 15000)
      }
    }
    const visibility = () => {
      clearTimeout(timer)
      if (!document.hidden) void poll()
    }
    document.addEventListener('visibilitychange', visibility)
    void poll()
    return () => {
      stopped = true
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [busy, client])

  useEffect(() => {
    const completed = tasks.filter((task) => !task.hidden && task.status === 'completed' && task.endedAt !== undefined)
    if (!completed.length) return
    const delay = Math.max(0, Math.min(...completed.map((task) => task.endedAt! + 10000 - Date.now())))
    const timer = setTimeout(() => {
      for (const task of completed) if (task.endedAt! + 10000 <= Date.now()) dismiss(task.id)
    }, delay)
    return () => clearTimeout(timer)
  }, [tasks, dismiss])

  const addJob = useCallback<Tasks['addJob']>((jobId, label = 'Background task') => {
    const id = `job-${jobId}`
    if (dismissed.current.has(id)) return
    setTasks((previous) => previous.some((task) => task.id === id) ? previous : [
      ...previous, { id, jobId, label, status: 'queued' },
    ])
  }, [])

  const trackTask = useCallback<Tasks['trackTask']>(async (label, promise, options) => {
    const id = `local-${++sequence.current}`
    const task: Task = { id, label, status: 'queued', link: options?.link }
    setTasks((previous) => [...previous, task])
    const started = setTimeout(() => setTasks((previous) => previous.map((item) => item.id === id ? { ...item, status: 'running' } : item)), 0)
    try {
      const value = await promise
      setTasks((previous) => previous.map((item) => item.id === id ? { ...item, status: 'completed', endedAt: Date.now() } : item))
      return value
    } catch (error) {
      setTasks((previous) => previous.map((item) => item.id === id ? { ...item, status: 'failed', reason: safeReason(error), endedAt: Date.now() } : item))
      throw error
    } finally {
      clearTimeout(started)
    }
  }, [])
  const value = useMemo(() => ({ tasks: tasks.filter((task) => !task.hidden), busy, hasFailed, trackTask, addJob, dismiss }), [tasks, busy, hasFailed, trackTask, addJob, dismiss])
  return <TaskContext.Provider value={value}>{children}</TaskContext.Provider>
}
