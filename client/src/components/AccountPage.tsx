import { useEffect, useState } from 'react'
import type { AuthUser } from '../hooks/useAuth'
import './AccountPage.css'

// ── Types ─────────────────────────────────────────────────────────────────

export interface UserProfile {
  nickname:   string | null
  jobTitle:   string | null
  department: string | null
}

interface AccountPageProps {
  user:   AuthUser
  onBack: () => void
  onNicknameChange: (name: string | null) => void
}

// ── AccountPage ───────────────────────────────────────────────────────────

export default function AccountPage({ user, onBack, onNicknameChange }: AccountPageProps) {
  const [loading,    setLoading]    = useState(true)
  const [saving,     setSaving]     = useState(false)
  const [saved,      setSaved]      = useState(false)
  const [error,      setError]      = useState<string | null>(null)

  const [nickname,   setNickname]   = useState('')
  const [jobTitle,   setJobTitle]   = useState('')
  const [department, setDepartment] = useState('')

  // Load profile on mount
  useEffect(() => {
    fetch('/api/me', { credentials: 'include' })
      .then((r) => r.json() as Promise<UserProfile & AuthUser>)
      .then((data) => {
        setNickname(data.nickname   ?? '')
        setJobTitle(data.jobTitle   ?? '')
        setDepartment(data.department ?? '')
      })
      .catch(() => setError('Failed to load profile.'))
      .finally(() => setLoading(false))
  }, [])

  async function handleSave(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      const r = await fetch('/api/me', {
        method:      'PATCH',
        headers:     { 'Content-Type': 'application/json' },
        credentials: 'include',
        body:        JSON.stringify({ nickname, jobTitle, department }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      onNicknameChange(nickname.trim() || null)
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch {
      setError('Failed to save. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  const displayName = nickname.trim() || user.name

  return (
    <div className="ap-shell">

      {/* ── Page header ─────────────────────────────────────────── */}
      <header className="ap-header">
        <button className="ap-back" onClick={onBack} aria-label="Back to chat">
          <BackIcon />
          <span>Back</span>
        </button>
        <h1 className="ap-title">My Account</h1>
      </header>

      {/* ── Page body ───────────────────────────────────────────── */}
      <div className="ap-body">

        {/* ── Identity card ─────────────────────────────────────── */}
        <section className="ap-section ap-identity-card">
          {user.picture ? (
            <img
              className="ap-avatar"
              src={user.picture}
              alt={displayName}
              referrerPolicy="no-referrer"
            />
          ) : (
            <div className="ap-avatar ap-avatar--fallback">
              {user.name.charAt(0).toUpperCase()}
            </div>
          )}
          <div className="ap-identity-info">
            <span className="ap-identity-name">{displayName}</span>
            <span className="ap-identity-email">{user.email}</span>
            <span className="ap-identity-badge">
              <GoogleBadge /> Connected with Google
            </span>
          </div>
        </section>

        {/* ── Profile settings ──────────────────────────────────── */}
        <section className="ap-section">
          <h2 className="ap-section-title">Profile</h2>
          <p className="ap-section-desc">
            Customise how your name appears across Booth Radar.
          </p>

          {loading ? (
            <div className="ap-loader"><span className="spinner" /></div>
          ) : (
            <form className="ap-form" onSubmit={handleSave}>

              <div className="ap-field">
                <label className="ap-label" htmlFor="ap-nickname">
                  Nickname
                </label>
                <p className="ap-field-hint">
                  Shown in the sidebar instead of your Google display name.
                </p>
                <input
                  id="ap-nickname"
                  className="ap-input"
                  type="text"
                  maxLength={48}
                  placeholder={user.name}
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  disabled={saving}
                  autoFocus
                />
              </div>

              <div className="ap-row">
                <div className="ap-field">
                  <label className="ap-label" htmlFor="ap-jobtitle">Job Title</label>
                  <input
                    id="ap-jobtitle"
                    className="ap-input"
                    type="text"
                    maxLength={64}
                    placeholder="e.g. Operations Manager"
                    value={jobTitle}
                    onChange={(e) => setJobTitle(e.target.value)}
                    disabled={saving}
                  />
                </div>

                <div className="ap-field">
                  <label className="ap-label" htmlFor="ap-department">Department</label>
                  <input
                    id="ap-department"
                    className="ap-input"
                    type="text"
                    maxLength={64}
                    placeholder="e.g. Marketing"
                    value={department}
                    onChange={(e) => setDepartment(e.target.value)}
                    disabled={saving}
                  />
                </div>
              </div>

              {error && <p className="ap-error">{error}</p>}

              <div className="ap-actions">
                <button type="submit" className="ap-btn ap-btn--save" disabled={saving}>
                  {saving
                    ? <><span className="spinner spinner--sm" /> Saving…</>
                    : saved
                    ? <><CheckIcon /> Saved</>
                    : 'Save changes'}
                </button>
              </div>

            </form>
          )}
        </section>

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

function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  )
}

function GoogleBadge() {
  return (
    <svg width="12" height="12" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
    </svg>
  )
}
