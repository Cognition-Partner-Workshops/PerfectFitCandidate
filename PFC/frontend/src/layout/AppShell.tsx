import { useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Bell,
  Briefcase,
  ChevronsLeft,
  ChevronsRight,
  IdCard,
  LayoutGrid,
  LogOut,
  MailCheck,
  MessageSquare,
  RefreshCw,
  Settings,
  Shield,
  Target,
  Users,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { useAuth, LAST_ROUTE_KEY } from '../auth/context'
import { visibleNav } from '../lib/permissions'
import { initials } from '../lib/format'
import { appVersion } from '../config'
import { api } from '../api'
import { Modal } from '../components/ui'
import { DISCLAIMER } from '../lib/csv'

const ICONS: Record<string, ReactNode> = {
  dashboard: <LayoutGrid size={18} aria-hidden />,
  demands: <Briefcase size={18} aria-hidden />,
  bench: <Users size={18} aria-hidden />,
  matches: <Target size={18} aria-hidden />,
  candidates: <IdCard size={18} aria-hidden />,
  email: <MailCheck size={18} aria-hidden />,
  chat: <MessageSquare size={18} aria-hidden />,
  settings: <Settings size={18} aria-hidden />,
  occ: <Shield size={18} aria-hidden />,
}

const IDLE_MS = 60 * 60_000
const WARN_MS = 60_000

/** Idle timeout of 60 min with a 60 s warning (REQ-AUTH-04, GUI_OCC_DESIGN.md §6.3). */
function useIdleTimeout(onExpire: () => void) {
  const [warning, setWarning] = useState(false)
  const last = useRef(0)
  const reset = useCallback(() => {
    last.current = Date.now()
    setWarning(false)
  }, [])
  useEffect(() => {
    last.current = Date.now()
    const events = ['mousedown', 'keydown', 'scroll', 'touchstart']
    const onActivity = () => {
      if (Date.now() - last.current < IDLE_MS - WARN_MS) last.current = Date.now()
    }
    events.forEach((e) => window.addEventListener(e, onActivity, { passive: true }))
    const timer = window.setInterval(() => {
      const idle = Date.now() - last.current
      if (idle >= IDLE_MS) onExpire()
      else if (idle >= IDLE_MS - WARN_MS) setWarning(true)
    }, 5_000)
    return () => {
      events.forEach((e) => window.removeEventListener(e, onActivity))
      window.clearInterval(timer)
    }
  }, [onExpire])
  return { warning, reset }
}

export function AppShell() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('pfc.navCollapsed') === '1')
  const emails = useQuery({ queryKey: ['emails'], queryFn: () => api.listEmails() })
  const drafts = emails.data?.items.filter((e) => e.status === 'Draft').length ?? 0

  useEffect(() => {
    localStorage.setItem(LAST_ROUTE_KEY, location.pathname + location.search)
  }, [location])

  const expire = useCallback(() => {
    void logout('timeout').then(() => navigate('/login'))
  }, [logout, navigate])
  const { warning, reset } = useIdleTimeout(expire)

  if (!user) return null
  const nav = visibleNav(user.role)
  const main = nav.filter((n) => !n.adminSection)
  const admin = nav.filter((n) => n.adminSection)
  const toggle = () => {
    setCollapsed((c) => {
      localStorage.setItem('pfc.navCollapsed', c ? '0' : '1')
      return !c
    })
  }

  return (
    <div className={`shell${collapsed ? ' collapsed' : ''}`}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <NavLink to="/dashboard" className="brand">
          <span className="logo">PFC</span>
          <span>PerfectFitCandidate</span>
        </NavLink>
        <div className="search" role="search">
          <label className="sr-only" htmlFor="global-search">
            Search requisitions, people, skills
          </label>
          <input id="global-search" placeholder="/  Search requisitions, people, skills…" />
        </div>
        <span className="spacer" />
        <span className="row" title="DL 04 Nov 2026 09:12 · CT 04 Nov 2026 09:20 · Local model loaded · Outlook detected">
          <RefreshCw size={16} aria-hidden /> <span className="small">Sync</span> <span className="status-dot" aria-label="All healthy" />
        </span>
        <button className="icon-btn" aria-label={`Notifications (${drafts} drafts awaiting approval)`}>
          <Bell size={18} />
        </button>
        <div className="user-chip">
          <span className="avatar" aria-hidden>
            {initials(user.fullName)}
          </span>
          <span>
            <div className="small">{user.fullName}</div>
            <div className="role" data-testid="role-label">
              {user.role}
            </div>
          </span>
          <button className="icon-btn" aria-label="Sign out" onClick={() => void logout().then(() => navigate('/login'))}>
            <LogOut size={18} />
          </button>
        </div>
      </header>
      <nav className="leftnav" aria-label="Main">
        {main.map((item) => (
          <NavLink key={item.id} to={item.path} title={collapsed ? item.label : undefined}>
            {ICONS[item.id]}
            <span className="label">{item.label}</span>
            {item.id === 'email' && drafts > 0 && <span className="badge">{drafts}</span>}
          </NavLink>
        ))}
        {admin.length > 0 && <div className="divider" role="separator" />}
        {admin.map((item) => (
          <NavLink key={item.id} to={item.path} title={collapsed ? item.label : undefined}>
            {ICONS[item.id]}
            <span className="label">{item.label}</span>
            <span className="caption">Admin</span>
          </NavLink>
        ))}
        <span className="spacer" />
        <button className="btn btn-tertiary btn-sm" onClick={toggle} aria-label={collapsed ? 'Expand panel' : 'Collapse panel'}>
          {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          {!collapsed && 'Collapse'}
        </button>
      </nav>
      <main id="main" className="main">
        <Outlet />
      </main>
      <footer className="footer">
        v{appVersion} · Local model: Qwen3-4B-Instruct-2507 · {DISCLAIMER}
      </footer>
      {warning && (
        <Modal
          title="You'll be signed out in 60 s"
          onClose={reset}
          actions={
            <>
              <button className="btn" onClick={expire}>
                Sign out
              </button>
              <button className="btn btn-primary" onClick={reset}>
                Stay signed in
              </button>
            </>
          }
        >
          <p>For security, PFC signs you out after 60 minutes of inactivity.</p>
        </Modal>
      )}
    </div>
  )
}
