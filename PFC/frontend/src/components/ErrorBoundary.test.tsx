import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

function ThrowingChild(): never {
  throw new Error('Scoring template crashed')
}

describe('ErrorBoundary', () => {
  it('shows a page error message instead of a blank page', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    try {
      render(
        <ErrorBoundary>
          <ThrowingChild />
        </ErrorBoundary>,
      )
      expect(screen.getByRole('alert').textContent).toBe(
        "This page couldn't be displayed: Scoring template crashed. Other OCC pages still work.",
      )
    } finally {
      consoleError.mockRestore()
    }
  })
})
