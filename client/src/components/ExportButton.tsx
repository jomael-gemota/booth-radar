import { useState } from 'react'
import './ExportButton.css'

interface Props {
  jobId: string
}

export default function ExportButton({ jobId }: Props) {
  const [loading, setLoading] = useState(false)

  async function handleExport() {
    setLoading(true)
    try {
      const res = await fetch(`/api/jobs/${jobId}/export`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `booth-radar-${jobId}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      console.error('export failed', err)
    } finally {
      setLoading(false)
    }
  }

  return (
    <button className="export-btn" onClick={handleExport} disabled={loading}>
      {loading ? 'Exporting…' : '⬇ Export CSV'}
    </button>
  )
}
