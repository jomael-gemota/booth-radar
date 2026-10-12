import ChatBox from './components/ChatBox'
import './App.css'

export default function App() {
  return (
    <main className="app">
      <header className="app-header">
        <span className="app-logo">📡</span>
        <h1>Booth Radar</h1>
        <p className="app-sub">Chat with Booth Radar to collect exhibitor brands &amp; booth locations from trade-show sites.</p>
      </header>
      <ChatBox />
    </main>
  )
}
