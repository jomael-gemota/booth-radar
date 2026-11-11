import { useEffect, useRef } from 'react'

/** One results-table row; column order is the key order of the first row. */
export type ResultRow = Record<string, string>

export interface Attachment {
  id: string
  name: string
  size: number
}

interface ThinkingPayload  { text: string }
interface TokenPayload     { text: string }
interface StatusPayload    { text: string; progress?: boolean }
interface ResultsPayload   { results: ResultRow[] }
interface DonePayload      { reply: string; results: ResultRow[] }
interface ErrorPayload     { message: string }

type Handler = {
  onThinking?:  (text: string) => void
  onToken:      (text: string) => void
  onStatus:     (text: string, progress: boolean) => void
  onResults:    (results: ResultRow[]) => void
  onFile:       (file: Attachment) => void
  onDone:       (results: ResultRow[]) => void
  onError:      (message: string) => void
  onCancelled?: () => void
  /** The turn ended while the stream was disconnected; reload the conversation. */
  onExpired?:   () => void
}

// Waits before each reconnect attempt after the stream drops
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 16000]

/**
 * Opens an SSE stream for one assistant turn and calls handler callbacks.
 * A dropped stream reconnects and receives only the events it missed.
 * The stream closes on `done`, `error`, or when turnId changes.
 */
export function useStream(turnId: string | null, handlers: Handler) {
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers

  useEffect(() => {
    if (!turnId) return

    let es: EventSource | null = null
    let lastEventId = 0
    let failures = 0
    let retry: ReturnType<typeof setTimeout> | undefined
    let closed = false

    function close() {
      closed = true
      clearTimeout(retry)
      es?.close()
    }

    function connect() {
      const source = new EventSource(`/api/stream/${turnId}${lastEventId ? `?after=${lastEventId}` : ''}`)
      es = source

      function on(type: string, handle: (data: string) => void) {
        source.addEventListener(type, (e) => {
          const event = e as MessageEvent<string>
          if (event.lastEventId) lastEventId = Number(event.lastEventId)
          failures = 0
          handle(event.data)
        })
      }

      on('thinking', (data) => handlersRef.current.onThinking?.((JSON.parse(data) as ThinkingPayload).text))
      on('token', (data) => handlersRef.current.onToken((JSON.parse(data) as TokenPayload).text))
      on('status', (data) => {
        const { text, progress } = JSON.parse(data) as StatusPayload
        handlersRef.current.onStatus(text, Boolean(progress))
      })
      on('results', (data) => handlersRef.current.onResults((JSON.parse(data) as ResultsPayload).results))
      on('file', (data) => handlersRef.current.onFile(JSON.parse(data) as Attachment))
      on('done', (data) => {
        close()
        handlersRef.current.onDone((JSON.parse(data) as DonePayload).results)
      })
      on('cancelled', () => {
        close()
        handlersRef.current.onCancelled?.()
      })
      on('expired', (data) => {
        close()
        if (handlersRef.current.onExpired) handlersRef.current.onExpired()
        else handlersRef.current.onError((JSON.parse(data) as ErrorPayload).message)
      })

      source.addEventListener('error', (e) => {
        source.close()
        if (closed) return
        if (e instanceof MessageEvent) {
          close()
          handlersRef.current.onError((JSON.parse(e.data) as ErrorPayload).message)
          return
        }
        // The connection dropped (proxy timeout, network blip): the turn keeps running.
        if (failures >= RECONNECT_DELAYS_MS.length) {
          close()
          handlersRef.current.onError('Connection lost')
          return
        }
        retry = setTimeout(connect, RECONNECT_DELAYS_MS[failures++])
      })
    }

    connect()
    return close
  }, [turnId])
}
