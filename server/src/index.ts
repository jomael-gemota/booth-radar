import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import { WebSocketServer, WebSocket } from 'ws';
import http from 'http';
import path from 'path';
import { ObjectId } from 'mongodb';
import { connectDb, getDb } from './db.js';
import { scrapeRouter } from './routes/scrape.js';

const PORT = Number(process.env.PORT ?? 3001);
const CLIENT_ORIGIN = process.env.CLIENT_ORIGIN ?? 'http://localhost:5173';
const IS_PROD = process.env.NODE_ENV === 'production';

// ── SSE subscriber map ─────────────────────────────────────────────────────
// jobId → Set of SSE response objects
export const sseClients = new Map<string, Set<express.Response>>();

// ── Worker WebSocket reference ─────────────────────────────────────────────
export let workerSocket: WebSocket | null = null;

async function main() {
  await connectDb();

  const app = express();

  // In dev, allow Vite's dev server origin; in prod, client is served from same origin
  if (!IS_PROD) {
    app.use(cors({ origin: CLIENT_ORIGIN }));
  }

  app.use(express.json());

  app.use('/api', scrapeRouter);

  app.get('/api/health', (_req, res) => {
    res.json({ ok: true, workerConnected: workerSocket !== null });
  });

  // ── Serve built React app in production ────────────────────────────────
  if (IS_PROD) {
    const clientDist = path.join(__dirname, '../../client/dist');
    app.use(express.static(clientDist));
    // Catch-all: serve index.html for client-side routing
    app.get('*', (_req, res) => {
      res.sendFile(path.join(clientDist, 'index.html'));
    });
  }

  const server = http.createServer(app);

  // ── WebSocket server (worker connects here) ────────────────────────────
  const wss = new WebSocketServer({ server, path: '/ws' });

  wss.on('connection', (ws) => {
    console.log('[ws] worker connected');
    workerSocket = ws;

    ws.on('message', (raw) => {
      try {
        const msg: WorkerMessage = JSON.parse(raw.toString());
        const { jobId, type, payload } = msg;

        // Persist terminal states to MongoDB
        if (type === 'done') {
          const db = getDb();
          db.collection('jobs').updateOne(
            { _id: new ObjectId(jobId) },
            { $set: { status: 'done', results: (payload as { results: unknown }).results } }
          ).catch((err) => console.error('[db] failed to update job', err));
        } else if (type === 'error') {
          const db = getDb();
          db.collection('jobs').updateOne(
            { _id: new ObjectId(jobId) },
            { $set: { status: 'error', error: (payload as { message: string }).message } }
          ).catch((err) => console.error('[db] failed to update job', err));
        }

        const clients = sseClients.get(jobId);
        if (!clients) return;

        for (const res of clients) {
          res.write(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`);
        }

        if (type === 'done' || type === 'error') {
          // Close all SSE connections for this job after delivery
          setTimeout(() => {
            const set = sseClients.get(jobId);
            if (set) {
              for (const res of set) {
                res.end();
              }
              sseClients.delete(jobId);
            }
          }, 500);
        }
      } catch (err) {
        console.error('[ws] failed to parse worker message', err);
      }
    });

    ws.on('close', () => {
      console.log('[ws] worker disconnected');
      workerSocket = null;
    });

    ws.on('error', (err) => {
      console.error('[ws] worker error', err);
      workerSocket = null;
    });
  });

  server.listen(PORT, () => {
    console.log(`[server] listening on http://localhost:${PORT}`);
  });
}

main().catch((err) => {
  console.error('[server] fatal', err);
  process.exit(1);
});

// ── Shared types ───────────────────────────────────────────────────────────
export interface WorkerMessage {
  jobId: string;
  type: 'token' | 'done' | 'error';
  payload: unknown;
}
