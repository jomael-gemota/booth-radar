import { Router, Request, Response } from 'express';
import { ObjectId } from 'mongodb';
import { getDb } from '../db.js';
import { sseClients, workerSocket } from '../index.js';

export const scrapeRouter = Router();

// ── POST /api/scrape ───────────────────────────────────────────────────────
// Body: { url: string }
// Returns: { jobId: string }
scrapeRouter.post('/scrape', async (req: Request, res: Response) => {
  const { url } = req.body as { url?: string };

  if (!url || typeof url !== 'string') {
    res.status(400).json({ error: 'url is required' });
    return;
  }

  if (!workerSocket) {
    res.status(503).json({ error: 'Worker is not connected. Please start the worker and try again.' });
    return;
  }

  const db = getDb();
  const col = db.collection('jobs');

  const job = {
    url,
    status: 'pending' as const,
    createdAt: new Date(),
    results: null as null | BoothEntry[],
    error: null as null | string,
  };

  const { insertedId } = await col.insertOne(job);
  const jobId = insertedId.toHexString();

  // Dispatch to worker
  workerSocket.send(JSON.stringify({ type: 'scrape', jobId, url }));

  res.json({ jobId });
});

// ── GET /api/jobs/:id ──────────────────────────────────────────────────────
scrapeRouter.get('/jobs/:id', async (req: Request, res: Response) => {
  const db = getDb();
  let oid: ObjectId;
  try {
    oid = new ObjectId(req.params.id);
  } catch {
    res.status(400).json({ error: 'invalid job id' });
    return;
  }

  const job = await db.collection('jobs').findOne({ _id: oid });
  if (!job) {
    res.status(404).json({ error: 'not found' });
    return;
  }

  res.json(job);
});

// ── GET /api/stream/:id ────────────────────────────────────────────────────
// Server-Sent Events — relays worker tokens to the browser
scrapeRouter.get('/stream/:id', (req: Request, res: Response) => {
  const jobId = req.params.id;

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();

  if (!sseClients.has(jobId)) {
    sseClients.set(jobId, new Set());
  }
  sseClients.get(jobId)!.add(res);

  // Keep-alive ping every 20 s
  const ping = setInterval(() => {
    res.write(': ping\n\n');
  }, 20_000);

  req.on('close', () => {
    clearInterval(ping);
    sseClients.get(jobId)?.delete(res);
    if (sseClients.get(jobId)?.size === 0) {
      sseClients.delete(jobId);
    }
  });
});

// ── GET /api/jobs/:id/export ───────────────────────────────────────────────
// Returns the scraping result as a CSV file
scrapeRouter.get('/jobs/:id/export', async (req: Request, res: Response) => {
  const db = getDb();
  let oid: ObjectId;
  try {
    oid = new ObjectId(req.params.id);
  } catch {
    res.status(400).json({ error: 'invalid job id' });
    return;
  }

  const job = await db.collection('jobs').findOne({ _id: oid });
  if (!job) {
    res.status(404).json({ error: 'not found' });
    return;
  }
  if (!job.results || !Array.isArray(job.results)) {
    res.status(409).json({ error: 'results not ready yet' });
    return;
  }

  const rows = job.results as BoothEntry[];
  const csv = buildCsv(rows);

  const filename = `booth-radar-${req.params.id}.csv`;
  res.setHeader('Content-Type', 'text/csv');
  res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
  res.send(csv);
});

// ── Helpers ────────────────────────────────────────────────────────────────

export interface BoothEntry {
  brand: string;
  booth: string;
}

function escapeCsvField(value: string): string {
  if (/[",\n\r]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

function buildCsv(rows: BoothEntry[]): string {
  const header = 'Brand,Booth\n';
  const body = rows
    .map((r) => `${escapeCsvField(r.brand)},${escapeCsvField(r.booth)}`)
    .join('\n');
  return header + body;
}
