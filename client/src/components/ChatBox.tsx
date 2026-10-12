import { useCallback, useEffect, useRef, useState } from 'react'
import { useStream, type BoothEntry } from '../hooks/useStream'
import ExportButton from './ExportButton'
import './ChatBox.css'

// ── Types ──────────────────────────────────────────────────────────────────

type Role = 'user' | 'assistant' | 'system'

interface Message {
  id: string
  role: Role
  text: string
  streaming?: boolean
}

// ── Helpers ────────────────────────────────────────────────────────────────

function genId() {
  return Math.random().toString(36).slice(2)
}

const URL_RE = /^https?:\/\/.+/i

function isUrl(s: string) {
  try {
    new URL(s)
    return URL_RE.test(s)
  } catch {
    return false
  }
}

// ── Component ──────────────────────────────────────────────────────────────

export default function ChatBox() {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: 'welcome',
      role: 'assistant',
      text: "Hi! I'm Booth Radar. Paste an exhibitor website URL and I'll scrape the brand and booth listings for you.",
    },
  ])
  const [input, setInput] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)
  const [results, setResults] = useState<BoothEntry[] | null>(null)
  const [busy, setBusy] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const streamingMsgId = useRef<string | null>(null)
  const workerOnline = useRef<boolean | null>(null)

  // Keep-alive health check on mount
  useEffect(() => {
    fetch('/api/health')
      .then((r) => r.json())
      .then((d: { workerConnected: boolean }) => {
        workerOnline.current = d.workerConnected
        if (!d.workerConnected) {
          addMessage({
            id: genId(),
            role: 'system',
            text: '⚠️ The scraper worker is currently offline. Jobs will fail until it reconnects.',
          })
        }
      })
      .catch(() => {
        workerOnline.current = false
      })
  }, [])

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  function addMessage(msg: Message) {
    setMessages((prev) => [...prev, msg])
  }

  function appendToken(text: string) {
    setMessages((prev) =>
      prev.map((m) =>
        m.id === streamingMsgId.current ? { ...m, text: m.text + text } : m
      )
    )
  }

  const handleDone = useCallback((res: BoothEntry[]) => {
    setResults(res)
    setBusy(false)
    // Mark streaming message as finished
    setMessages((prev) =>
      prev.map((m) =>
        m.id === streamingMsgId.current ? { ...m, streaming: false } : m
      )
    )
    streamingMsgId.current = null
    // Add summary message
    addMessage({
      id: genId(),
      role: 'assistant',
      text: `✅ Done! Found **${res.length}** exhibitor${res.length !== 1 ? 's' : ''}. You can export the results below.`,
    })
  }, [])

  const handleError = useCallback((message: string) => {
    setBusy(false)
    setMessages((prev) =>
      prev.map((m) =>
        m.id === streamingMsgId.current ? { ...m, streaming: false } : m
      )
    )
    streamingMsgId.current = null
    addMessage({ id: genId(), role: 'system', text: `❌ ${message}` })
  }, [])

  useStream(jobId, {
    onToken: appendToken,
    onDone: handleDone,
    onError: handleError,
  })

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const url = input.trim()
    if (!url || busy) return

    if (!isUrl(url)) {
      addMessage({
        id: genId(),
        role: 'system',
        text: "That doesn't look like a valid URL. Please include the full address (e.g. https://example.com/exhibitors).",
      })
      return
    }

    // Show user message
    addMessage({ id: genId(), role: 'user', text: url })
    setInput('')
    setBusy(true)
    setResults(null)

    // Kick off thinking message
    const thinkingId = genId()
    streamingMsgId.current = thinkingId
    addMessage({ id: thinkingId, role: 'assistant', text: '', streaming: true })

    try {
      const res = await fetch('/api/scrape', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      })

      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { error?: string }
        throw new Error(body.error ?? `HTTP ${res.status}`)
      }

      const { jobId: newJobId } = (await res.json()) as { jobId: string }
      setJobId(newJobId)
    } catch (err) {
      handleError((err as Error).message)
    }
  }

  return (
    <section className="chatbox">
      <div className="chat-messages">
        {messages.map((msg) => (
          <div key={msg.id} className={`bubble bubble--${msg.role}`}>
            <MessageContent text={msg.text} streaming={msg.streaming} />
          </div>
        ))}

        {results && results.length > 0 && jobId && (
          <div className="results-block">
            <ResultsTable results={results} />
            <ExportButton jobId={jobId} />
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <form className="chat-input-row" onSubmit={handleSubmit}>
        <input
          className="chat-input"
          type="text"
          placeholder="Paste exhibitor website URL…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
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

function MessageContent({ text, streaming }: { text: string; streaming?: boolean }) {
  // Render **bold** markdown only
  const rendered = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  return (
    <span
      className={streaming ? 'streaming-text' : ''}
      dangerouslySetInnerHTML={{ __html: rendered || (streaming ? '<span class="cursor-blink">▌</span>' : '') }}
    />
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
