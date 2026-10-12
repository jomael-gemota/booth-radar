import { useEffect, useRef } from 'react'

export interface BoothEntry {
  brand: string
  booth: string
}

interface TokenPayload   { text: string }
interface StatusPayload  { text: string }
interface ResultsPayload { results: BoothEntry[] }
interface DonePayload    { reply: string; results: BoothEntry[] }
interface ErrorPayload   { message: string }

type Handler = {
  onToken:   (text: string) => void
  onStatus:  (text: string) => void
  onResults: (results: BoothEntry[]) => void
  onDone:    (results: BoothEntry[]) => void
  onError:   (message: string) => void
}

/**
 * Opens an SSE stream for one assistant turn and calls handler callbacks.
 * The stream closes on `done`, `error`, or when turnId changes.
 */
export function useStream(turnId: string | null, handlers: Handler) {
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers

  useEffect(() => {
    if (!turnId) return

    const es = new EventSource(`/api/stream/${turnId}`)

    es.addEventListener('token', (e) => {
      handlersRef.current.onToken((JSON.parse(e.data) as TokenPayload).text)
    })

    es.addEventListener('status', (e) => {
      handlersRef.current.onStatus((JSON.parse(e.data) as StatusPayload).text)
    })

    es.addEventListener('results', (e) => {
      handlersRef.current.onResults((JSON.parse(e.data) as ResultsPayload).results)
    })

    es.addEventListener('done', (e) => {
      es.close()
      handlersRef.current.onDone((JSON.parse(e.data) as DonePayload).results)
    })

    es.addEventListener('error', (e) => {
      es.close()
      if (e instanceof MessageEvent) {
        handlersRef.current.onError((JSON.parse(e.data) as ErrorPayload).message)
      } else {
        handlersRef.current.onError('Connection lost')
      }
    })

    return () => es.close()
  }, [turnId])
}
