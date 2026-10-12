import ChatBox from './components/ChatBox'
import './App.css'

export default function App() {
  return (
    <main className="app">
      <header className="app-header">
        <span className="app-logo">📡</span>
        <h1>Booth Radar</h1>
        <p className="app-sub">Paste an exhibitor website URL to extract brand &amp; booth listings.</p>
      </header>
      <ChatBox />
    </main>
  )
}
