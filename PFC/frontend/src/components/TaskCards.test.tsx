import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { TaskCards } from './TaskCards'
import { TaskContext, taskStatus } from '../lib/tasks'
import type { Task } from '../lib/tasks'

describe('task cards', () => {
  it('shows three cards, expands extras, minimizes, exposes links and keyboard dismissal', () => {
    const tasks: Task[] = [
      { id: '1', label: 'DL upload: demand.xlsx', status: 'completed' },
      { id: '2', label: 'CT upload: tracker.xlsx', status: 'queued' },
      { id: '3', label: 'CV Sync for 3 profiles', status: 'running', progress: 0.45, link: '/#/bench' },
      { id: '4', label: 'JD analyze T10004', status: 'failed', reason: 'Check the input and retry.' },
    ]
    const dismiss = vi.fn()
    render(<TaskContext.Provider value={{ tasks, dismiss, addJob: vi.fn(), trackTask: (_label, promise) => promise }}><TaskCards /></TaskContext.Provider>)
    expect(screen.getByRole('region', { name: 'Task status' }).getAttribute('aria-live')).toBe('polite')
    expect(screen.queryByText('DL upload: demand.xlsx: Completed')).toBeNull()
    expect(screen.getByRole('link', { name: 'CV Sync for 3 profiles: In Progress (45%)' }).getAttribute('href')).toBe('/#/bench')
    expect(screen.getByText('JD analyze T10004: Failed — Check the input and retry.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '+1 more' }))
    expect(screen.getByText('DL upload: demand.xlsx: Completed')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Minimize tasks' }))
    expect(screen.queryByRole('link')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show tasks (4)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss CT upload: tracker.xlsx' }))
    expect(dismiss).toHaveBeenCalledWith('2')
    fireEvent.keyDown(screen.getByRole('link'), { key: 'Escape' })
    expect(dismiss).toHaveBeenCalledWith('3')
  })

  it('renders empty and cancelled states and clamps percentages', () => {
    const { rerender } = render(<TaskCards />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(taskStatus({ id: '1', label: 'Job', status: 'cancelled' })).toBe('Cancelled')
    expect(taskStatus({ id: '1', label: 'Job', status: 'running', progress: 2 })).toBe('In Progress (100%)')
    rerender(<TaskContext.Provider value={{ tasks: [{ id: '1', label: 'CV Sync', status: 'cancelled' }], dismiss: vi.fn(), addJob: vi.fn(), trackTask: (_label, promise) => promise }}><TaskCards /></TaskContext.Provider>)
    expect(screen.getByText('CV Sync: Cancelled')).toBeTruthy()
  })
})
