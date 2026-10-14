import { useAuth } from './hooks/useAuth'
import SignInPage from './components/SignInPage'
import ChatBox from './components/ChatBox'

export default function App() {
  const auth = useAuth()

  if (auth.status === 'loading') {
    return (
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100dvh',
        background: 'var(--main-bg)',
      }}>
        <span className="spinner" aria-label="Loading…" />
      </div>
    )
  }

  if (auth.status === 'unauthenticated') {
    // Pass authFailed=true when Google redirects back with ?auth=failed
    const authFailed = new URLSearchParams(window.location.search).get('auth') === 'failed'
    return <SignInPage authFailed={authFailed} />
  }

  return <ChatBox user={auth.user} />
}
