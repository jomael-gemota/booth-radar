import { useCallback, useEffect, useRef, useState } from 'react'
import { useStream, type BoothEntry } from '../hooks/useStream'
import ExportButton from './ExportButton'
import './ChatBox.css'

// ── Types ──────────────────────────────────────────────────────────────────

type Role = 'user' | 'assistant' | 'status' | 'system' | 'thinking'

interface Message {
  id: string
  role: Role
  text: string
  streaming?: boolean
}

interface ConversationResponse {
  id: string
  messages: { role: 'user' | 'assistant'; content: string }[]
  results: BoothEntry[]
  activeTurnId: string | null
}

// ── Helpers ────────────────────────────────────────────────────────────────

const STORAGE_KEY = 'booth-radar:conversationId'

const WELCOME: Message = {
  id: 'welcome',
  role: 'assistant',
  text:
    "Hi! I'm Booth Radar. Tell me which trade-show site to look at and what you need, for example:\n" +
    '• "Get all exhibitors and booths from https://www.atlanta-apparel.com/exhibitor/Directory, every letter A–Z"\n' +
    '• "Just scrape letters A to C for now"\n' +
    'I\'ll collect the brands and booths into a table you can export as CSV.',
}

function genId() {
  return Math.random().toString(36).slice(2)
}

// ── Component ──────────────────────────────────────────────────────────────

export default function ChatBox() {
  const [messages, setMessages] = useState<Message[]>([WELCOME])
  const [input, setInput] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(
    () => localStorage.getItem(STORAGE_KEY),
  )
  const [turnId, setTurnId] = useState<string | null>(null)
  const [results, setResults] = useState<BoothEntry[]>([])
  const [busy, setBusy] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const streamingMsgId = useRef<string | null>(null)
  const thinkingMsgId = useRef<string | null>(null)

  const addMessage = useCallback((msg: Message) => {
    setMessages((prev) => [...prev, msg])
  }, [])

  // Worker health check on mount
  useEffect(() => {
    fetch('/api/health')
      .then((r) => r.json())
      .then((d: { workerConnected: boolean }) => {
        if (!d.workerConnected) {
          addMessage({
            id: genId(),
            role: 'system',
            text: '⚠️ The Booth Radar worker is currently offline. Messages will fail until it reconnects.',
          })
        }
      })
      .catch(() => {})
  }, [addMessage])

  // Restore the previous conversation on mount
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (!saved) return
    fetch(`/api/conversations/${saved}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json() as Promise<ConversationResponse>
      })
      .then((c) => {
        setMessages((prev) => [
          WELCOME,
          ...c.messages.map((m) => ({ id: genId(), role: m.role, text: m.content })),
          ...prev.filter((m) => m.role === 'system'),
        ])
        setResults(c.results)
        if (c.activeTurnId) {
          setBusy(true)
          setTurnId(c.activeTurnId)
        }
      })
      .catch(() => {
        localStorage.removeItem(STORAGE_KEY)
        setConversationId(null)
      })
  }, [])

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, results])

  // ── Stream handlers ──────────────────────────────────────────────────────

  function finishStreaming() {
    const id = streamingMsgId.current
    streamingMsgId.current = null
    if (id) {
      setMessages((prev) =>
        prev
          .filter((m) => m.id !== id || m.text.trim() !== '')
          .map((m) => (m.id === id ? { ...m, streaming: false } : m)),
      )
    }

    // Also stop streaming on the thinking bubble (collapses it)
    const tid = thinkingMsgId.current
    thinkingMsgId.current = null
    if (tid) {
      setMessages((prev) =>
        prev.map((m) => (m.id === tid ? { ...m, streaming: false } : m)),
      )
    }
  }

  function handleThinking(text: string) {
    const tid = thinkingMsgId.current
    if (!tid) {
      const newId = genId()
      thinkingMsgId.current = newId
      addMessage({ id: newId, role: 'thinking', text, streaming: true })
      return
    }
    setMessages((prev) => prev.map((m) => (m.id === tid ? { ...m, text: m.text + text } : m)))
  }

  function handleToken(text: string) {
    const id = streamingMsgId.current
    if (!id) {
      const newId = genId()
      streamingMsgId.current = newId
      addMessage({ id: newId, role: 'assistant', text, streaming: true })
      return
    }
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, text: m.text + text } : m)))
  }

  function handleStatus(text: string) {
    finishStreaming()
    addMessage({ id: genId(), role: 'status', text })
  }

  function endTurn() {
    finishStreaming()
    setBusy(false)
    setTurnId(null)
  }

  useStream(turnId, {
    onThinking: handleThinking,
    onToken: handleToken,
    onStatus: handleStatus,
    onResults: setResults,
    onDone: (res) => {
      setResults(res)
      endTurn()
    },
    onError: (message) => {
      endTurn()
      addMessage({ id: genId(), role: 'system', text: `❌ ${message}` })
    },
  })

  // ── Actions ──────────────────────────────────────────────────────────────

  async function send() {
    const text = input.trim()
    if (!text || busy) return

    addMessage({ id: genId(), role: 'user', text })
    setInput('')
    setBusy(true)

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversationId, message: text }),
      })
      const body = (await res.json().catch(() => ({}))) as {
        error?: string
        conversationId?: string
        turnId?: string
      }
      if (!res.ok || !body.conversationId || !body.turnId) {
        throw new Error(body.error ?? `HTTP ${res.status}`)
      }
      setConversationId(body.conversationId)
      localStorage.setItem(STORAGE_KEY, body.conversationId)
      setTurnId(body.turnId)
    } catch (err) {
      setBusy(false)
      addMessage({ id: genId(), role: 'system', text: `❌ ${(err as Error).message}` })
    }
  }

  function newChat() {
    localStorage.removeItem(STORAGE_KEY)
    streamingMsgId.current = null
    thinkingMsgId.current = null
    setConversationId(null)
    setTurnId(null)
    setBusy(false)
    setResults([])
    setMessages([WELCOME])
    setInput('')
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    void send()
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void send()
    }
  }

  const last = messages[messages.length - 1]
  const showTyping = busy && !(last?.role === 'assistant' && last.streaming)

  return (
    <section className="chatbox">
      <div className="chat-toolbar">
        <span className="chat-toolbar-title">
          {results.length > 0 ? `${results.length} row${results.length !== 1 ? 's' : ''} collected` : 'New conversation'}
        </span>
        <button className="chat-new" type="button" onClick={newChat}>
          + New chat
        </button>
      </div>

      <div className="chat-messages">
        {messages.map((msg) => (
          <div key={msg.id} className={`bubble bubble--${msg.role}`}>
            {msg.role === 'thinking' ? (
              <ThinkingBubble text={msg.text} streaming={msg.streaming} />
            ) : (
              <MessageContent text={msg.text} streaming={msg.streaming} />
            )}
          </div>
        ))}

        {showTyping && (
          <div className="bubble bubble--assistant">
            <span className="typing" aria-label="Booth Radar is thinking">
              <span />
              <span />
              <span />
            </span>
          </div>
        )}

        {results.length > 0 && conversationId && (
          <div className="results-block">
            <ResultsTable results={results} />
            <ExportButton conversationId={conversationId} />
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <form className="chat-input-row" onSubmit={handleSubmit}>
        <textarea
          className="chat-input"
          placeholder="Message Booth Radar… (Shift+Enter for a new line)"
          value={input}
          rows={Math.min(6, Math.max(1, input.split('\n').length))}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={busy}
          autoFocus
        />
        <button className="chat-send" type="submit" disabled={busy || !input.trim()}>
          {busy ? <Spinner /> : 'Send'}
        </button>
      </form>
    </section>
  )
}

// ── Sub-components ─────────────────────────────────────────────────────────

function escapeHtml(s: string) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function renderMarkdown(text: string) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(https?:\/\/[^\s<]+[^\s<.,;:!?)\]'"])/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>')
}

function MessageContent({ text, streaming }: { text: string; streaming?: boolean }) {
  return (
    <span
      className={streaming ? 'streaming-text' : ''}
      dangerouslySetInnerHTML={{
        __html: renderMarkdown(text) + (streaming ? '<span class="cursor-blink">▌</span>' : ''),
      }}
    />
  )
}

function ThinkingBubble({ text, streaming }: { text: string; streaming?: boolean }) {
  const [expanded, setExpanded] = useState(!!streaming)

  // Auto-expand while streaming, collapse when done
  useEffect(() => {
    if (streaming) setExpanded(true)
    else setExpanded(false)
  }, [streaming])

  return (
    <div className="thinking-bubble">
      <button
        type="button"
        className="thinking-toggle"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
      >
        <span className={`thinking-icon${streaming ? ' thinking-icon--spin' : ''}`}>🧠</span>
        <span className="thinking-label">
          {streaming ? 'Thinking…' : 'View reasoning'}
        </span>
        <span className="thinking-chevron">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && (
        <div className="thinking-content">
          <MessageContent text={text} streaming={streaming} />
        </div>
      )}
    </div>
  )
}

function ResultsTable({ results }: { results: BoothEntry[] }) {
  return (
    <div className="results-table-wrap">
      <table className="results-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Brand</th>
            <th>Booth</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r, i) => (
            <tr key={i}>
              <td>{i + 1}</td>
              <td>{r.brand}</td>
              <td>{r.booth}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Spinner() {
  return <span className="spinner" aria-label="Loading" />
}
