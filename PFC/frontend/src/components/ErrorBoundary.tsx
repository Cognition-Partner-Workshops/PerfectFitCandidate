import { Component, type ReactNode } from 'react'
import { Banner } from './ui'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  error: Error | null
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <Banner tone="danger">
          This page couldn&apos;t be displayed: {this.state.error.message}. Other OCC pages still work.
        </Banner>
      )
    }
    return this.props.children
  }
}
