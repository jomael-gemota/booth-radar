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

interface ConversationSummary {
  id: string
  title: string
  preview: string
  createdAt: number
}

interface ConversationResponse {
  id: string
  messages: { role: 'user' | 'assistant'; content: string }[]
  results: BoothEntry[]
  activeTurnId: string | null
}

// ── Storage keys ────────────────────────────────────────────────────────────

const CONV_LIST_KEY   = 'booth-radar:conversations'
const ACTIVE_CONV_KEY = 'booth-radar:conversationId'

// ── Welcome message ──────────────────────────────────────────────────────────

const WELCOME: Message = {
  id: 'welcome',
  role: 'assistant',
  text:
    "Hi! I'm **Booth Radar**. Tell me which trade-show site to look at and what you need, for example:\n\n" +
    '• "Get all exhibitors and booths from https://www.atlanta-apparel.com/exhibitor/Directory, every letter A–Z"\n' +
    '• "Just scrape letters A to C for now"\n\n' +
    "I'll collect the brands and booths into a table you can export as CSV.",
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function genId() {
  return Math.random().toString(36).slice(2)
}

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n) + '…' : s
}

function getConversations(): ConversationSummary[] {
  try {
    return JSON.parse(localStorage.getItem(CONV_LIST_KEY) ?? '[]')
  } catch {
    return []
  }
}

function saveConversations(list: ConversationSummary[]) {
  localStorage.setItem(CONV_LIST_KEY, JSON.stringify(list))
}

function groupByDate(convs: ConversationSummary[]) {
  const todayStart     = new Date().setHours(0, 0, 0, 0)
  const yesterdayStart = todayStart - 86_400_000
  const weekStart      = todayStart - 7  * 86_400_000
  const monthStart     = todayStart - 30 * 86_400_000

  const groups: { label: string; items: ConversationSummary[] }[] = []

  const push = (label: string, items: ConversationSummary[]) => {
    if (items.length) groups.push({ label, items })
  }

  push('Today',            convs.filter((c) => c.createdAt >= todayStart))
  push('Yesterday',        convs.filter((c) => c.createdAt >= yesterdayStart && c.createdAt < todayStart))
  push('Previous 7 Days',  convs.filter((c) => c.createdAt >= weekStart      && c.createdAt < yesterdayStart))
  push('Previous 30 Days', convs.filter((c) => c.createdAt >= monthStart     && c.createdAt < weekStart))
  push('Older',            convs.filter((c) => c.createdAt < monthStart))

  return groups
}

// ── ChatBox ─────────────────────────────────────────────────────────────────

export default function ChatBox() {
  // Sidebar
  const [conversations, setConversations] = useState<ConversationSummary[]>(getConversations)
  const [sidebarOpen, setSidebarOpen]     = useState(true)

  // Active conversation
  const [messages,        setMessages]        = useState<Message[]>([WELCOME])
  const [input,           setInput]           = useState('')
  const [conversationId,  setConversationId]  = useState<string | null>(
    () => localStorage.getItem(ACTIVE_CONV_KEY),
  )
  const [turnId,   setTurnId]   = useState<string | null>(null)
  const [results,  setResults]  = useState<BoothEntry[]>([])
  const [busy,     setBusy]     = useState(false)
  const [loadingConv, setLoadingConv] = useState(false)

  const bottomRef      = useRef<HTMLDivElement>(null)
  const streamingMsgId = useRef<string | null>(null)
  const thinkingMsgId  = useRef<string | null>(null)
  const inputRef       = useRef<HTMLTextAreaElement>(null)

  const addMessage = useCallback((msg: Message) => {
    setMessages((prev) => [...prev, msg])
  }, [])

  // ── Worker health check ────────────────────────────────────────────────

  useEffect(() => {
    fetch('/api/health')
      .then((r) => r.json())
      .then((d: { workerConnected: boolean }) => {
        if (!d.workerConnected) {
          addMessage({
            id:   genId(),
            role: 'system',
            text: '⚠️ The Booth Radar worker is currently offline. Messages will fail until it reconnects.',
          })
        }
      })
      .catch(() => {})
  }, [addMessage])

  // ── Restore active conversation on mount ───────────────────────────────

  useEffect(() => {
    const saved = localStorage.getItem(ACTIVE_CONV_KEY)
    if (!saved) return
    void loadConversation(saved, true)
  }, []) // loadConversation is stable — only runs once on mount

  // ── Auto-scroll ────────────────────────────────────────────────────────

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, results])

  // ── Load a conversation by id ──────────────────────────────────────────

  async function loadConversation(id: string, onMount = false) {
    setLoadingConv(true)
    try {
      const r = await fetch(`/api/conversations/${id}`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const c: ConversationResponse = await r.json()
      streamingMsgId.current = null
      thinkingMsgId.current  = null
      setMessages([
        WELCOME,
        ...c.messages.map((m) => ({ id: genId(), role: m.role as Role, text: m.content })),
      ])
      setResults(c.results)
      setConversationId(id)
      localStorage.setItem(ACTIVE_CONV_KEY, id)
      if (c.activeTurnId) {
        setBusy(true)
        setTurnId(c.activeTurnId)
      }
    } catch {
      if (onMount) {
        localStorage.removeItem(ACTIVE_CONV_KEY)
        setConversationId(null)
      }
    } finally {
      setLoadingConv(false)
    }
  }

  // ── Stream handlers ────────────────────────────────────────────────────

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
    onToken:    handleToken,
    onStatus:   handleStatus,
    onResults:  setResults,
    onDone: (res) => {
      setResults(res)
      endTurn()
    },
    onError: (message) => {
      endTurn()
      addMessage({ id: genId(), role: 'system', text: `❌ ${message}` })
    },
  })

  // ── Send message ───────────────────────────────────────────────────────

  async function send() {
    const text = input.trim()
    if (!text || busy) return

    addMessage({ id: genId(), role: 'user', text })
    setInput('')
    setBusy(true)

    try {
      const res = await fetch('/api/chat', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ conversationId, message: text }),
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
      localStorage.setItem(ACTIVE_CONV_KEY, body.conversationId)
      setTurnId(body.turnId)

      // Track in sidebar — add if it's a brand-new conversation
      if (!conversations.find((c) => c.id === body.conversationId)) {
        const summary: ConversationSummary = {
          id:        body.conversationId!,
          title:     truncate(text, 42),
          preview:   truncate(text, 80),
          createdAt: Date.now(),
        }
        const updated = [summary, ...conversations]
        setConversations(updated)
        saveConversations(updated)
      }
    } catch (err) {
      setBusy(false)
      addMessage({ id: genId(), role: 'system', text: `❌ ${(err as Error).message}` })
    }
  }

  function newChat() {
    localStorage.removeItem(ACTIVE_CONV_KEY)
    streamingMsgId.current = null
    thinkingMsgId.current  = null
    setConversationId(null)
    setTurnId(null)
    setBusy(false)
    setResults([])
    setMessages([WELCOME])
    setInput('')
    inputRef.current?.focus()
  }

  function deleteConversation(id: string, e: React.MouseEvent) {
    e.stopPropagation()
    const updated = conversations.filter((c) => c.id !== id)
    setConversations(updated)
    saveConversations(updated)
    if (conversationId === id) newChat()
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

  const last      = messages[messages.length - 1]
  const showTyping = busy && !(last?.role === 'assistant' && last.streaming)
  const activeTitle = conversationId
    ? (conversations.find((c) => c.id === conversationId)?.title ?? 'Conversation')
    : 'New chat'

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div className="app-shell">

      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside className={`sidebar${sidebarOpen ? '' : ' sidebar--collapsed'}`}>

        <div className="sidebar-top">
          <div className="sidebar-brand">
            <span className="sidebar-brand-icon">📡</span>
            {sidebarOpen && <span className="sidebar-brand-name">Booth Radar</span>}
          </div>
          <button
            className="sidebar-collapse-btn"
            onClick={() => setSidebarOpen((o) => !o)}
            title={sidebarOpen ? 'Collapse sidebar' : 'Expand sidebar'}
          >
            {sidebarOpen ? <CollapseIcon /> : <ExpandIcon />}
          </button>
        </div>

        <button className="new-chat-btn" onClick={newChat} title="New chat">
          <PlusIcon />
          {sidebarOpen && <span>New chat</span>}
        </button>

        {sidebarOpen && (
          <nav className="conv-list" aria-label="Conversation history">
            {conversations.length === 0 ? (
              <p className="conv-empty">No conversations yet</p>
            ) : (
              groupByDate(conversations).map((group) => (
                <div key={group.label} className="conv-group">
                  <p className="conv-group-label">{group.label}</p>
                  {group.items.map((conv) => (
                    <button
                      key={conv.id}
                      className={`conv-item${conv.id === conversationId ? ' conv-item--active' : ''}`}
                      onClick={() => void loadConversation(conv.id)}
                      title={conv.title}
                    >
                      <span className="conv-item-title">{conv.title}</span>
                      <span
                        className="conv-item-delete"
                        role="button"
                        tabIndex={0}
                        onClick={(e) => deleteConversation(conv.id, e)}
                        onKeyDown={(e) => e.key === 'Enter' && deleteConversation(conv.id, e as unknown as React.MouseEvent)}
                        title="Delete conversation"
                        aria-label="Delete conversation"
                      >
                        ✕
                      </span>
                    </button>
                  ))}
                </div>
              ))
            )}
          </nav>
        )}

        {sidebarOpen && (
          <div className="sidebar-footer">
            <span className="sidebar-footer-text">Booth Radar · Internal Tool</span>
          </div>
        )}
      </aside>

      {/* ── Main panel ──────────────────────────────────────────────── */}
      <main className="main-panel">

        {/* Header */}
        <header className="chat-header">
          {!sidebarOpen && (
            <button
              className="sidebar-open-btn"
              onClick={() => setSidebarOpen(true)}
              title="Open sidebar"
            >
              <MenuIcon />
            </button>
          )}
          <h1 className="chat-header-title">{activeTitle}</h1>
          {results.length > 0 && (
            <span className="chat-header-badge">
              {results.length} row{results.length !== 1 ? 's' : ''}
            </span>
          )}
        </header>

        {/* Messages */}
        <div className="chat-messages">
          {loadingConv && (
            <div className="conv-loading">
              <span className="spinner spinner--sm" />
              Loading conversation…
            </div>
          )}

          {messages.map((msg) => (
            <MessageBubble key={msg.id} msg={msg} />
          ))}

          {showTyping && <TypingIndicator />}

          {results.length > 0 && conversationId && (
            <div className="results-block">
              <p className="results-label">
                📊 {results.length} exhibitor{results.length !== 1 ? 's' : ''} collected
              </p>
              <ResultsTable results={results} />
              <ExportButton conversationId={conversationId} />
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div className="input-area">
          <form className="input-form" onSubmit={handleSubmit}>
            <div className="input-wrapper">
              <textarea
                ref={inputRef}
                className="chat-input"
                placeholder="Message Booth Radar… (Shift+Enter for new line)"
                value={input}
                rows={Math.min(6, Math.max(1, input.split('\n').length))}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={busy}
                autoFocus
              />
              <button className="send-btn" type="submit" disabled={busy || !input.trim()}>
                {busy ? <Spinner /> : <SendIcon />}
              </button>
            </div>
            <p className="input-hint">
              Booth Radar can make mistakes. Verify important data before use.
            </p>
          </form>
        </div>

      </main>
    </div>
  )
}

// ── MessageBubble ────────────────────────────────────────────────────────────

function MessageBubble({ msg }: { msg: Message }) {
  if (msg.role === 'thinking') {
    return <ThinkingBubble text={msg.text} streaming={msg.streaming} />
  }
  if (msg.role === 'status') {
    return (
      <div className="msg-status">
        <span className="msg-status-dot" />
        <span>{msg.text}</span>
      </div>
    )
  }
  if (msg.role === 'system') {
    return (
      <div className="msg-system">
        <span>{msg.text}</span>
      </div>
    )
  }
  if (msg.role === 'user') {
    return (
      <div className="msg-row msg-row--user">
        <div className="msg-bubble msg-bubble--user">
          <MessageContent text={msg.text} />
        </div>
      </div>
    )
  }
  // assistant
  return (
    <div className="msg-row msg-row--assistant">
      <div className="msg-avatar" aria-hidden>📡</div>
      <div className="msg-bubble msg-bubble--assistant">
        <MessageContent text={msg.text} streaming={msg.streaming} />
      </div>
    </div>
  )
}

// ── Markdown rendering ───────────────────────────────────────────────────────

function escapeHtml(s: string) {
  return s
    .replace(/&/g,  '&amp;')
    .replace(/</g,  '&lt;')
    .replace(/>/g,  '&gt;')
    .replace(/"/g,  '&quot;')
    .replace(/'/g,  '&#39;')
}

function renderMarkdown(text: string) {
  let html = escapeHtml(text)

  // Fenced code blocks  ``` ... ```
  html = html.replace(/```([\s\S]*?)```/g, (_, code: string) =>
    `<pre class="code-block"><code>${code.replace(/^\n/, '')}</code></pre>`,
  )

  // Inline code  `...`
  html = html.replace(/`([^`\n]+)`/g, '<code class="code-inline">$1</code>')

  // Bold  **...**
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')

  // Auto-linked URLs
  html = html.replace(
    /(https?:\/\/[^\s<"]+[^\s<.,;:!?)\]'"&])/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>',
  )

  // Line breaks
  html = html.replace(/\n/g, '<br/>')

  return html
}

function MessageContent({ text, streaming }: { text: string; streaming?: boolean }) {
  return (
    <span
      className={`msg-content${streaming ? ' msg-content--streaming' : ''}`}
      dangerouslySetInnerHTML={{
        __html:
          renderMarkdown(text) +
          (streaming ? '<span class="cursor-blink">▌</span>' : ''),
      }}
    />
  )
}

// ── ThinkingBubble ───────────────────────────────────────────────────────────

function ThinkingBubble({ text, streaming }: { text: string; streaming?: boolean }) {
  const [expanded, setExpanded] = useState(!!streaming)

  useEffect(() => {
    if (streaming) setExpanded(true)
    else           setExpanded(false)
  }, [streaming])

  return (
    <div className="thinking-wrap">
      <button
        type="button"
        className="thinking-header"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
      >
        <span className={`thinking-orb${streaming ? ' thinking-orb--pulse' : ''}`} />
        <span className="thinking-label">
          {streaming ? 'Thinking…' : 'View reasoning'}
        </span>
        <span className="thinking-chevron" aria-hidden>
          {expanded ? '▲' : '▼'}
        </span>
      </button>

      {expanded && (
        <div className="thinking-body">
          <MessageContent text={text} streaming={streaming} />
        </div>
      )}
    </div>
  )
}

// ── TypingIndicator ──────────────────────────────────────────────────────────

function TypingIndicator() {
  return (
    <div className="msg-row msg-row--assistant">
      <div className="msg-avatar" aria-hidden>📡</div>
      <div className="msg-bubble msg-bubble--assistant">
        <span className="typing-dots" aria-label="Booth Radar is thinking">
          <span /><span /><span />
        </span>
      </div>
    </div>
  )
}

// ── ResultsTable ─────────────────────────────────────────────────────────────

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

// ── Icon components ──────────────────────────────────────────────────────────

function SendIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  )
}

function CollapseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  )
}

function ExpandIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="9 18 15 12 9 6" />
    </svg>
  )
}

function MenuIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round">
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="21" y2="18" />
    </svg>
  )
}

function Spinner() {
  return <span className="spinner" aria-label="Loading" />
}
