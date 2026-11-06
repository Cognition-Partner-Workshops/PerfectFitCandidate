import { lazy, Suspense } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { createQueryClient } from './lib/queryClient'
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthProvider'
import { RequireAuth, RequireRole } from './auth/guards'
import { AppShell } from './layout/AppShell'
import { LoginPage } from './pages/auth/LoginPage'
import { SetupPage } from './pages/auth/SetupPage'
import { ForcedPasswordChange } from './pages/auth/ForcedPasswordChange'
import { DemandListPage } from './pages/DemandListPage'
import { BenchListPage } from './pages/BenchListPage'
import { MatchResultsPage } from './pages/MatchResultsPage'
import { CandidateProfilePage } from './pages/CandidateProfilePage'
import { EmailApprovalPage } from './pages/EmailApprovalPage'
import { SettingsPage } from './pages/settings/SettingsPage'
import { OccPage } from './pages/occ/OccPage'
import { Spinner } from './components/ui'

// ECharts lives in its own chunk, loaded with the Dashboard.
const DashboardPage = lazy(() => import('./pages/DashboardPage').then((m) => ({ default: m.DashboardPage })))

export function AppRoutes() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/setup" element={<SetupPage />} />
        <Route
          path="/change-password"
          element={
            <RequireAuth>
              <ForcedPasswordChange />
            </RequireAuth>
          }
        />
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route
            path="/dashboard"
            element={
              <Suspense fallback={<Spinner />}>
                <DashboardPage />
              </Suspense>
            }
          />
          <Route path="/demands" element={<DemandListPage />} />
          <Route path="/bench" element={<BenchListPage />} />
          <Route path="/matches" element={<MatchResultsPage />} />
          <Route path="/candidates" element={<CandidateProfilePage />} />
          <Route path="/candidates/:candidateId" element={<CandidateProfilePage />} />
          <Route path="/email" element={<EmailApprovalPage />} />
          <Route path="/settings/*" element={<SettingsPage />} />
          <Route
            path="/occ/*"
            element={
              <RequireRole role="Admin">
                <OccPage />
              </RequireRole>
            }
          />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      </Routes>
    </AuthProvider>
  )
}

const queryClient = createQueryClient()

/** Hash routes give stable deep links such as `#/matches?req=T1A123` (GUI_OCC_DESIGN.md §4.3). */
function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <AppRoutes />
      </HashRouter>
    </QueryClientProvider>
  )
}

export default App
