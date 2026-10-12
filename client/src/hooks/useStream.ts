import { useEffect, useRef } from 'react'

export type SseEventType = 'token' | 'done' | 'error'

export interface TokenPayload  { text: string }
export interface DonePayload   { results: BoothEntry[] }
export interface ErrorPayload  { message: string }

export interface BoothEntry {
  brand: string
  booth: string
}

type Handler = {
  onToken: (text: string) => void
  onDone:  (results: BoothEntry[]) => void
  onError: (message: string) => void
}

/**
 * Opens an SSE stream for the given jobId and calls handler callbacks.
 * Returns a cleanup function that closes the stream.
 */
export function useStream(jobId: string | null, handlers: Handler) {
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers

  useEffect(() => {
    if (!jobId) return

    const es = new EventSource(`/api/stream/${jobId}`)

    es.addEventListener('token', (e) => {
      const { text } = JSON.parse(e.data) as TokenPayload
      handlersRef.current.onToken(text)
    })

    es.addEventListener('done', (e) => {
      const { results } = JSON.parse(e.data) as DonePayload
      handlersRef.current.onDone(results)
      es.close()
    })

    es.addEventListener('error', (e) => {
      if (e instanceof MessageEvent) {
        const { message } = JSON.parse(e.data) as ErrorPayload
        handlersRef.current.onError(message)
      } else {
        handlersRef.current.onError('Connection lost')
      }
      es.close()
    })

    return () => es.close()
  }, [jobId])
}
