import { useEffect, useState, useCallback } from 'react'
import type { AuthUser } from '../hooks/useAuth'
import './UserLogsPage.css'

// ── Types ─────────────────────────────────────────────────────────────────

interface UserEntry {
  id:         string
  name:       string
  email:      string
  picture:    string | null
  nickname:   string | null
  jobTitle:   string | null
  department: string | null
  lastSeenAt: string | null   // ISO date string from server
  online:     boolean
}

interface UserLogsPageProps {
  user:   AuthUser
  onBack: () => void
}

// ── Helpers ───────────────────────────────────────────────────────────────

function formatRelative(iso: string | null): string {
  if (!iso) return 'Never'
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60)  return 'Just now'
  const m = Math.floor(s / 60)
  if (m < 60)  return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24)  return `${h}h ago`
  const d = Math.floor(h / 24)
  return `${d}d ago`
}

// ── UserLogsPage ──────────────────────────────────────────────────────────

export default function UserLogsPage({ onBack }: UserLogsPageProps) {
  const [users,     setUsers]     = useState<UserEntry[]>([])
  const [loading,   setLoading]   = useState(true)
  const [error,     setError]     = useState<string | null>(null)
  const [lastFetch, setLastFetch] = useState<Date | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const fetchUsers = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true)
    else        setRefreshing(true)
    setError(null)
    try {
      const r = await fetch('/api/admin/users', { credentials: 'include' })
      if (r.status === 403) { setError('Access denied.'); return }
      if (!r.ok)            { throw new Error(`HTTP ${r.status}`) }
      const data = (await r.json()) as { users: UserEntry[] }
      setUsers(data.users)
      setLastFetch(new Date())
    } catch {
      setError('Failed to load user logs. Please try again.')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  // Initial fetch
  useEffect(() => { void fetchUsers() }, [fetchUsers])

  // Auto-refresh every 30 seconds
  useEffect(() => {
    const id = setInterval(() => void fetchUsers(true), 30_000)
    return () => clearInterval(id)
  }, [fetchUsers])

  const onlineCount  = users.filter((u) => u.online).length
  const offlineCount = users.length - onlineCount

  return (
    <div className="ul-shell">

      {/* ── Page header ────────────────────────────────────────── */}
      <header className="ul-header">
        <button className="ul-back" onClick={onBack} aria-label="Back to chat">
          <BackIcon />
          <span>Back</span>
        </button>
        <h1 className="ul-title">User Logs</h1>
        <div className="ul-header-actions">
          <button
            className={`ul-refresh-btn${refreshing ? ' ul-refresh-btn--spinning' : ''}`}
            onClick={() => void fetchUsers(true)}
            disabled={refreshing}
            title="Refresh"
            aria-label="Refresh user list"
          >
            <RefreshIcon />
          </button>
          {lastFetch && (
            <span className="ul-last-updated">
              Updated {formatRelative(lastFetch.toISOString())}
            </span>
          )}
        </div>
      </header>

      {/* ── Page body ──────────────────────────────────────────── */}
      <div className="ul-body">

        {loading ? (
          <div className="ul-loader"><span className="spinner" /></div>
        ) : error ? (
          <p className="ul-error">{error}</p>
        ) : (
          <>
            {/* ── Summary pills ──────────────────────────────────── */}
            <div className="ul-summary">
              <span className="ul-pill ul-pill--total">
                {users.length} user{users.length !== 1 ? 's' : ''} total
              </span>
              <span className="ul-pill ul-pill--online">
                <span className="ul-status-dot ul-status-dot--online" />
                {onlineCount} online
              </span>
              <span className="ul-pill ul-pill--offline">
                <span className="ul-status-dot ul-status-dot--offline" />
                {offlineCount} offline
              </span>
            </div>

            {/* ── User table ─────────────────────────────────────── */}
            {users.length === 0 ? (
              <p className="ul-empty">No user records found.</p>
            ) : (
              <div className="ul-table-wrap">
                <table className="ul-table">
                  <thead>
                    <tr>
                      <th>User</th>
                      <th>Job Title</th>
                      <th>Department</th>
                      <th>Status</th>
                      <th>Last Seen</th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.map((u) => (
                      <tr key={u.id} className={u.online ? 'ul-row--online' : ''}>
                        <td>
                          <div className="ul-user-cell">
                            {u.picture ? (
                              <img
                                className="ul-avatar"
                                src={u.picture}
                                alt={u.nickname ?? u.name}
                                referrerPolicy="no-referrer"
                              />
                            ) : (
                              <div className="ul-avatar ul-avatar--fallback">
                                {(u.nickname ?? u.name).charAt(0).toUpperCase()}
                              </div>
                            )}
                            <div className="ul-user-info">
                              <span className="ul-user-name">
                                {u.nickname ? (
                                  <>{u.nickname} <span className="ul-user-real">({u.name})</span></>
                                ) : u.name}
                              </span>
                              <span className="ul-user-email">{u.email}</span>
                            </div>
                          </div>
                        </td>
                        <td className="ul-meta">{u.jobTitle   || <span className="ul-none">—</span>}</td>
                        <td className="ul-meta">{u.department || <span className="ul-none">—</span>}</td>
                        <td>
                          {u.online ? (
                            <span className="ul-badge ul-badge--online">
                              <span className="ul-status-dot ul-status-dot--online" /> Online
                            </span>
                          ) : (
                            <span className="ul-badge ul-badge--offline">Offline</span>
                          )}
                        </td>
                        <td className="ul-lastseen">{formatRelative(u.lastSeenAt)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <p className="ul-footnote">
              Online = active within the last 1 minute. Refreshes automatically every 30 s.
            </p>
          </>
        )}
      </div>
    </div>
  )
}

// ── Icons ─────────────────────────────────────────────────────────────────

function BackIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  )
}

function RefreshIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 4 23 10 17 10" />
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
    </svg>
  )
}
