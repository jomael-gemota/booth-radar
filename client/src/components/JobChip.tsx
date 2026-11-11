import { useEffect, useRef, useState } from 'react'
import './JobChip.css'

type JobStatus = 'queued' | 'running' | 'paused' | 'done' | 'cancelled' | 'failed'

interface Job {
  jobId: string
  status: JobStatus
  fields: string[]
  total: number
  cursor: number
  rowsAdded: number
  failedCount: number
  message?: string
}

interface Props {
  conversationId: string | null
  /** Change it to look for a new job (e.g. after a turn that may have started one). */
  checkKey: number
  /** A batch finished: the conversation's results changed. */
  onProgress: () => void
  /** The job ended; *message* is the finish message the server added to the conversation. */
  onFinished: (message: string) => void
}

const POLL_MS = 3000
const ACTIVE: JobStatus[] = ['queued', 'running', 'paused']

function isActive(job: Job | null): boolean {
  return job !== null && ACTIVE.includes(job.status)
}

/** Progress and controls for the conversation's background job, polled while it is active. */
export default function JobChip({ conversationId, checkKey, onProgress, onFinished }: Props) {
  const [job, setJob] = useState<Job | null>(null)
  const [pending, setPending] = useState(false)
  const callbacks = useRef({ onProgress, onFinished })
  callbacks.current = { onProgress, onFinished }

  useEffect(() => {
    setJob(null)
    if (!conversationId) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let last: Job | null = null

    async function poll() {
      try {
        const r = await fetch(`/api/conversations/${conversationId}/job`)
        if (!r.ok || stopped) return
        const { job: next } = (await r.json()) as { job: Job | null }
        if (stopped) return
        if (last && next && last.jobId === next.jobId) {
          if (next.cursor !== last.cursor) callbacks.current.onProgress()
          if (isActive(last) && !isActive(next)) callbacks.current.onFinished(next.message ?? '')
        }
        last = next
        setJob(next)
        if (isActive(next)) timer = setTimeout(() => void poll(), POLL_MS)
      } catch {
        if (!stopped) timer = setTimeout(() => void poll(), POLL_MS)
      }
    }

    void poll()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [conversationId, checkKey])

  async function act(action: 'pause' | 'resume' | 'cancel') {
    if (!job || pending) return
    setPending(true)
    try {
      const r = await fetch(`/api/jobs/${job.jobId}/${action}`, { method: 'POST' })
      if (r.ok) {
        const { job: next } = (await r.json()) as { job: Job }
        setJob(next)
        if (!isActive(next)) {
          callbacks.current.onProgress()
          callbacks.current.onFinished(next.message ?? '')
        }
      }
    } finally {
      setPending(false)
    }
  }

  if (!job || !isActive(job)) return null

  const percent = job.total ? Math.min(100, Math.round((job.cursor / job.total) * 100)) : 0
  const what = job.fields.slice(0, 3).join(', ') + (job.fields.length > 3 ? '…' : '')
  const state =
    job.status === 'paused' ? 'Paused' : job.status === 'queued' ? 'Waiting to start' : 'Collecting'

  return (
    <div className={`job-chip job-chip--${job.status}`} role="status">
      <div className="job-chip-row">
        {job.status === 'running' && <span className="spinner spinner--sm" />}
        <span className="job-chip-text">
          <strong>{state}</strong> {what} · {job.cursor.toLocaleString()} / {job.total.toLocaleString()} pages
          · {job.rowsAdded.toLocaleString()} new rows
          {job.failedCount > 0 && <> · {job.failedCount.toLocaleString()} failed</>}
        </span>
        <span className="job-chip-actions">
          {job.status === 'paused' ? (
            <button type="button" disabled={pending} onClick={() => void act('resume')}>Resume</button>
          ) : (
            <button type="button" disabled={pending} onClick={() => void act('pause')}>Pause</button>
          )}
          <button type="button" disabled={pending} onClick={() => void act('cancel')}>Cancel</button>
        </span>
      </div>
      <div className="job-chip-bar">
        <div className="job-chip-bar-fill" style={{ width: `${percent}%` }} />
      </div>
    </div>
  )
}
