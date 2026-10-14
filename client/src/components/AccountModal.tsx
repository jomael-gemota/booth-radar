import { useEffect, useRef, useState } from 'react'
import type { AuthUser } from '../hooks/useAuth'
import './AccountModal.css'

// ── Types ─────────────────────────────────────────────────────────────────

export interface UserProfile {
  nickname:   string | null
  jobTitle:   string | null
  department: string | null
}

interface AccountModalProps {
  user:    AuthUser
  onClose: () => void
}

// ── AccountModal ──────────────────────────────────────────────────────────

export default function AccountModal({ user, onClose }: AccountModalProps) {
  const [profile,  setProfile]  = useState<UserProfile | null>(null)
  const [loading,  setLoading]  = useState(true)
  const [saving,   setSaving]   = useState(false)
  const [saved,    setSaved]    = useState(false)
  const [error,    setError]    = useState<string | null>(null)

  const [nickname,   setNickname]   = useState('')
  const [jobTitle,   setJobTitle]   = useState('')
  const [department, setDepartment] = useState('')

  const overlayRef = useRef<HTMLDivElement>(null)

  // Load profile on mount
  useEffect(() => {
    fetch('/api/me', { credentials: 'include' })
      .then((r) => r.json() as Promise<UserProfile & AuthUser>)
      .then((data) => {
        setProfile(data)
        setNickname(data.nickname   ?? '')
        setJobTitle(data.jobTitle   ?? '')
        setDepartment(data.department ?? '')
      })
      .catch(() => setError('Failed to load profile.'))
      .finally(() => setLoading(false))
  }, [])

  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Close on overlay click
  function handleOverlayClick(e: React.MouseEvent) {
    if (e.target === overlayRef.current) onClose()
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      const r = await fetch('/api/me', {
        method:  'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ nickname, jobTitle, department }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch {
      setError('Failed to save. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  const displayName = profile?.nickname?.trim() || user.name

  return (
    <div className="am-overlay" ref={overlayRef} onClick={handleOverlayClick} role="dialog" aria-modal="true" aria-label="My Account">
      <div className="am-card">

        {/* ── Header ──────────────────────────────────────────── */}
        <div className="am-header">
          <h2 className="am-title">My Account</h2>
          <button className="am-close" onClick={onClose} aria-label="Close">
            <CloseIcon />
          </button>
        </div>

        {/* ── Identity (read-only from Google) ────────────────── */}
        <div className="am-identity">
          {user.picture ? (
            <img
              className="am-avatar"
              src={user.picture}
              alt={displayName}
              referrerPolicy="no-referrer"
            />
          ) : (
            <div className="am-avatar am-avatar--fallback">
              {user.name.charAt(0).toUpperCase()}
            </div>
          )}
          <div className="am-identity-info">
            <span className="am-identity-name">{displayName}</span>
            <span className="am-identity-email">{user.email}</span>
          </div>
        </div>

        {/* ── Form ────────────────────────────────────────────── */}
        {loading ? (
          <div className="am-loading"><span className="spinner" /></div>
        ) : (
          <form className="am-form" onSubmit={handleSave}>
            <div className="am-field">
              <label className="am-label" htmlFor="am-nickname">
                Nickname
                <span className="am-label-hint">Shown in the sidebar instead of your full name</span>
              </label>
              <input
                id="am-nickname"
                className="am-input"
                type="text"
                maxLength={48}
                placeholder={user.name}
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
                disabled={saving}
              />
            </div>

            <div className="am-field">
              <label className="am-label" htmlFor="am-jobtitle">Job Title</label>
              <input
                id="am-jobtitle"
                className="am-input"
                type="text"
                maxLength={64}
                placeholder="e.g. Operations Manager"
                value={jobTitle}
                onChange={(e) => setJobTitle(e.target.value)}
                disabled={saving}
              />
            </div>

            <div className="am-field">
              <label className="am-label" htmlFor="am-department">Department</label>
              <input
                id="am-department"
                className="am-input"
                type="text"
                maxLength={64}
                placeholder="e.g. Marketing"
                value={department}
                onChange={(e) => setDepartment(e.target.value)}
                disabled={saving}
              />
            </div>

            {error && <p className="am-error">{error}</p>}

            <div className="am-actions">
              <button type="button" className="am-btn am-btn--cancel" onClick={onClose} disabled={saving}>
                Cancel
              </button>
              <button type="submit" className="am-btn am-btn--save" disabled={saving}>
                {saving ? <><span className="spinner spinner--sm" /> Saving…</> : saved ? '✓ Saved' : 'Save changes'}
              </button>
            </div>
          </form>
        )}

      </div>
    </div>
  )
}

// ── Icons ─────────────────────────────────────────────────────────────────

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}
