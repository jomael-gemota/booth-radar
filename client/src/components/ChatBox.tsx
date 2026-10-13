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
  pinned?: boolean
}

interface ConversationResponse {
  id: string
  messages: { role: 'user' | 'assistant' | 'thinking'; content: string }[]
  results: BoothEntry[]
  activeTurnId: string | null
}

// ── Storage keys ────────────────────────────────────────────────────────────

const CONV_LIST_KEY   = 'booth-radar:conversations'
const ACTIVE_CONV_KEY = 'booth-radar:conversationId'

// ── Welcome sentinel (used as array marker — never rendered as a bubble) ─────

const WELCOME: Message = { id: 'welcome', role: 'assistant', text: '' }

// ── Greeting helpers ──────────────────────────────────────────────────────────

/** Immediate client-side fallback — used only if the API call fails */
function getStaticGreeting(): { greeting: string; subtext: string } {
  const hour = new Date().getHours()
  if (hour >= 5  && hour < 12) return { greeting: "Good morning! I'm Booth Radar.", subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you." }
  if (hour >= 12 && hour < 17) return { greeting: "Good afternoon! I'm Booth Radar.", subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you." }
  if (hour >= 17 && hour < 21) return { greeting: "Good evening! I'm Booth Radar.", subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you." }
  return { greeting: "Good night! I'm Booth Radar.", subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you." }
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

  // ── Auto-resize textarea ───────────────────────────────────────────────
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'           // shrink first to get accurate scrollHeight
    el.style.height = `${el.scrollHeight}px`
  }, [input])

  // Refs for title generation (avoid stale closure issues)
  const conversationIdRef = useRef<string | null>(conversationId)
  const isFirstTurnRef    = useRef(false)

  useEffect(() => {
    conversationIdRef.current = conversationId
  }, [conversationId])

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
      conversationIdRef.current = id
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

  // ── Generate AI title for a new conversation ───────────────────────────

  async function generateTitle(convId: string) {
    try {
      const r = await fetch(`/api/conversations/${convId}/generate-title`, { method: 'POST' })
      if (!r.ok) return
      const { title } = (await r.json()) as { title: string }
      if (!title) return
      setConversations((prev) => {
        const updated = prev.map((c) => (c.id === convId ? { ...c, title } : c))
        saveConversations(updated)
        return updated
      })
    } catch {
      // Non-critical — fallback title stays
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
    // Generate a proper title for the first turn of a new conversation
    if (isFirstTurnRef.current && conversationIdRef.current) {
      void generateTitle(conversationIdRef.current)
      isFirstTurnRef.current = false
    }
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
      conversationIdRef.current = body.conversationId
      localStorage.setItem(ACTIVE_CONV_KEY, body.conversationId)
      setTurnId(body.turnId)

      // Track in sidebar — add if it's a brand-new conversation
      if (!conversations.find((c) => c.id === body.conversationId)) {
        isFirstTurnRef.current = true
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
    conversationIdRef.current = null
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

  function pinConversation(id: string, e: React.MouseEvent) {
    e.stopPropagation()
    const updated = conversations.map((c) =>
      c.id === id ? { ...c, pinned: !c.pinned } : c,
    )
    setConversations(updated)
    saveConversations(updated)
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

  const isWelcomeState  = messages.length === 1 && messages[0].id === 'welcome'
  const displayMessages = messages.filter((m) => m.id !== 'welcome')

  const last      = messages[messages.length - 1]
  const showTyping = busy && !(last?.role === 'assistant' && last.streaming)
  const activeTitle = conversationId
    ? (conversations.find((c) => c.id === conversationId)?.title ?? 'Conversation')
    : 'New chat'

  const pinnedConvs  = conversations.filter((c) => c.pinned)
  const unpinnedConvs = conversations.filter((c) => !c.pinned)

  // ── Shared input form renderer ─────────────────────────────────────────

  function renderInputForm() {
    return (
      <form className="input-form" onSubmit={handleSubmit}>
        <div className="input-wrapper">
          <textarea
            ref={inputRef}
            className="chat-input"
            placeholder="Ask Booth Radar"
            value={input}
            rows={1}
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
    )
  }

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div className="app-shell">

      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside className={`sidebar${sidebarOpen ? '' : ' sidebar--collapsed'}`}>

        <div className="sidebar-top">
          <div className="sidebar-brand">
            <RadarLogoIcon className="sidebar-brand-icon" />
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
              <>
                {/* ── Pinned section ─────────────────────────────── */}
                {pinnedConvs.length > 0 && (
                  <div className="conv-group">
                    <p className="conv-group-label">
                      <PinIcon className="conv-group-pin-icon" /> Pinned
                    </p>
                    {pinnedConvs.map((conv) => (
                      <ConvItem
                        key={conv.id}
                        conv={conv}
                        active={conv.id === conversationId}
                        onSelect={() => void loadConversation(conv.id)}
                        onPin={(e) => pinConversation(conv.id, e)}
                        onDelete={(e) => deleteConversation(conv.id, e)}
                      />
                    ))}
                  </div>
                )}

                {/* ── Date-grouped unpinned ──────────────────────── */}
                {groupByDate(unpinnedConvs).map((group) => (
                  <div key={group.label} className="conv-group">
                    <p className="conv-group-label">{group.label}</p>
                    {group.items.map((conv) => (
                      <ConvItem
                        key={conv.id}
                        conv={conv}
                        active={conv.id === conversationId}
                        onSelect={() => void loadConversation(conv.id)}
                        onPin={(e) => pinConversation(conv.id, e)}
                        onDelete={(e) => deleteConversation(conv.id, e)}
                      />
                    ))}
                  </div>
                ))}
              </>
            )}
          </nav>
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

        {isWelcomeState ? (
          /* ── Welcome / centered-input layout ─────────────────────── */
          <div className="welcome-center">
            <WelcomeScreen />
            <div className="input-area input-area--welcome">
              {renderInputForm()}
            </div>
          </div>
        ) : (
          /* ── Normal chat layout ───────────────────────────────────── */
          <>
            <div className="chat-messages">
              {loadingConv && (
                <div className="conv-loading">
                  <span className="spinner spinner--sm" />
                  Loading conversation…
                </div>
              )}

              {displayMessages.map((msg) => (
                <MessageBubble
                  key={msg.id}
                  msg={msg}
                  conversationId={conversationId ?? undefined}
                  hasResults={results.length > 0}
                />
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

            <div className="input-area">
              {renderInputForm()}
            </div>
          </>
        )}

      </main>
    </div>
  )
}

// ── ConvItem ─────────────────────────────────────────────────────────────────

interface ConvItemProps {
  conv: ConversationSummary
  active: boolean
  onSelect: () => void
  onPin: (e: React.MouseEvent) => void
  onDelete: (e: React.MouseEvent) => void
}

function ConvItem({ conv, active, onSelect, onPin, onDelete }: ConvItemProps) {
  return (
    <button
      className={`conv-item${active ? ' conv-item--active' : ''}`}
      onClick={onSelect}
      title={conv.title}
    >
      <span className="conv-item-title">{conv.title}</span>
      <span className="conv-item-actions">
        <span
          className={`conv-item-pin${conv.pinned ? ' conv-item-pin--active' : ''}`}
          role="button"
          tabIndex={0}
          onClick={onPin}
          onKeyDown={(e) => e.key === 'Enter' && onPin(e as unknown as React.MouseEvent)}
          title={conv.pinned ? 'Unpin conversation' : 'Pin conversation'}
          aria-label={conv.pinned ? 'Unpin conversation' : 'Pin conversation'}
        >
          <PinIcon />
        </span>
        <span
          className="conv-item-delete"
          role="button"
          tabIndex={0}
          onClick={onDelete}
          onKeyDown={(e) => e.key === 'Enter' && onDelete(e as unknown as React.MouseEvent)}
          title="Delete conversation"
          aria-label="Delete conversation"
        >
          ✕
        </span>
      </span>
    </button>
  )
}

// ── WelcomeScreen ────────────────────────────────────────────────────────────

function WelcomeScreen() {
  const [content, setContent]   = useState<{ greeting: string; subtext: string } | null>(null)
  const [visible, setVisible]   = useState(false)

  useEffect(() => {
    const hour = new Date().getHours()
    fetch(`/api/greeting?hour=${hour}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json() as Promise<{ greeting: string; subtext: string }>
      })
      .then((data) => {
        setContent(data)
      })
      .catch(() => {
        setContent(getStaticGreeting())
      })
  }, [])

  useEffect(() => {
    if (!content) return
    const t = setTimeout(() => setVisible(true), 40)
    return () => clearTimeout(t)
  }, [content])

  return (
    <div className="welcome-screen">
      <RadarLogoIcon className="welcome-icon" />
      <div className={`welcome-text${visible ? ' welcome-text--visible' : ''}`}>
        <h2 className="welcome-heading">
          {content?.greeting ?? ''}
        </h2>
        <p className="welcome-sub">
          {content?.subtext ?? ''}
        </p>
      </div>
    </div>
  )
}

// ── MessageBubble ────────────────────────────────────────────────────────────

interface MessageBubbleProps {
  msg: Message
  conversationId?: string
  hasResults?: boolean
}

function MessageBubble({ msg, conversationId, hasResults }: MessageBubbleProps) {
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
      <div className="msg-avatar" aria-hidden>
        <RadarLogoIcon />
      </div>
      <div className="msg-bubble-wrapper">
        <div className="msg-bubble msg-bubble--assistant">
          <MessageContent text={msg.text} streaming={msg.streaming} />
        </div>
        {!msg.streaming && msg.text.trim() && (
          <div className="msg-actions">
            <CopyButton text={msg.text} />
            {conversationId && hasResults && (
              <ExportCsvButton conversationId={conversationId} />
            )}
          </div>
        )}
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

  html = html.replace(/```([\s\S]*?)```/g, (_, code: string) =>
    `<pre class="code-block"><code>${code.replace(/^\n/, '')}</code></pre>`,
  )

  html = html.replace(/`([^`\n]+)`/g, '<code class="code-inline">$1</code>')

  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')

  html = html.replace(
    /(https?:\/\/[^\s<"]+[^\s<.,;:!?)\]'"&])/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>',
  )

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
  // Auto-expand while streaming; collapse (but keep accessible) once done
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
          <ChevronDownIcon />
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
      <div className="msg-avatar" aria-hidden>
        <RadarLogoIcon />
      </div>
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

// ── ExportCsvButton ───────────────────────────────────────────────────────────

function ExportCsvButton({ conversationId }: { conversationId: string }) {
  const [state, setState] = useState<'idle' | 'loading' | 'done'>('idle')

  async function handleExport() {
    if (state === 'loading') return
    setState('loading')
    try {
      const res = await fetch(`/api/conversations/${conversationId}/export`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement('a')
      a.href     = url
      a.download = `booth-radar-${conversationId}.csv`
      a.click()
      URL.revokeObjectURL(url)
      setState('done')
      setTimeout(() => setState('idle'), 2000)
    } catch (err) {
      console.error('export failed', err)
      setState('idle')
    }
  }

  return (
    <button
      type="button"
      className={`copy-btn${state === 'done' ? ' copy-btn--copied' : ''}`}
      onClick={handleExport}
      disabled={state === 'loading'}
      title={state === 'loading' ? 'Exporting…' : 'Export results to CSV'}
    >
      {state === 'done' ? <CheckIcon /> : <DownloadIcon />}
      <span className="copy-btn-label">
        {state === 'loading' ? 'Exporting…' : state === 'done' ? 'Exported' : 'Export CSV'}
      </span>
    </button>
  )
}

// ── CopyButton ───────────────────────────────────────────────────────────────

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  function handleCopy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }).catch(() => {})
  }

  return (
    <button
      type="button"
      className={`copy-btn${copied ? ' copy-btn--copied' : ''}`}
      onClick={handleCopy}
      title={copied ? 'Copied!' : 'Copy response'}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
      <span className="copy-btn-label">{copied ? 'Copied' : 'Copy'}</span>
    </button>
  )
}

// ── Icon & Logo components ───────────────────────────────────────────────────

function RadarLogoIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {/* Outer ring */}
      <circle cx="16" cy="16" r="13" stroke="currentColor" strokeWidth="1.5" opacity="0.25" />
      {/* Mid ring */}
      <circle cx="16" cy="16" r="8.5" stroke="currentColor" strokeWidth="1.5" opacity="0.5" />
      {/* Inner ring */}
      <circle cx="16" cy="16" r="4" stroke="currentColor" strokeWidth="1.5" opacity="0.75" />
      {/* Center dot */}
      <circle cx="16" cy="16" r="1.75" fill="currentColor" />
      {/* Sweep line */}
      <line x1="16" y1="16" x2="27" y2="10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.6" />
      {/* Blip — detected booth */}
      <circle cx="25.5" cy="10.5" r="2" fill="currentColor" opacity="0.9" />
    </svg>
  )
}

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

function PinIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="12" y1="17" x2="12" y2="22" />
      <path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z" />
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

function CopyIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
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

function ChevronDownIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  )
}
