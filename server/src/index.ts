import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import { WebSocketServer } from 'ws';
import http from 'http';
import path from 'path';
import fs from 'fs';
import { connectDb } from './db.js';
import { attachWorker, isWorkerConnected } from './relay.js';
import { chatRouter } from './routes/chat.js';

const PORT = Number(process.env.PORT ?? 3001);
const CLIENT_ORIGIN = process.env.CLIENT_ORIGIN ?? 'http://localhost:5173';
const IS_PROD = process.env.NODE_ENV === 'production';

async function main() {
  await connectDb();

  const app = express();

  // In dev (no client/dist), allow Vite's dev server origin for CORS
  if (!IS_PROD) {
    app.use(cors({ origin: CLIENT_ORIGIN }));
  }

  app.use(express.json());

  app.use('/api', chatRouter);

  app.get('/api/health', (_req, res) => {
    res.json({ ok: true, workerConnected: isWorkerConnected() });
  });

  // ── Serve built React app if client/dist exists ────────────────────────
  // Works automatically in production (Railway builds client/dist before start)
  // and is silently skipped in local dev (where Vite handles the client).
  const clientDist = path.join(__dirname, '../../client/dist');
  if (fs.existsSync(clientDist)) {
    console.log(`[server] serving static client from ${clientDist}`);
    app.use(express.static(clientDist));
    // Catch-all: return index.html for client-side routing
    app.get('*', (_req, res) => {
      res.sendFile(path.join(clientDist, 'index.html'));
    });
  } else {
    console.log('[server] client/dist not found — skipping static file serving (dev mode)');
  }

  const server = http.createServer(app);

  // ── WebSocket server (worker connects here) ────────────────────────────
  const wss = new WebSocketServer({ server, path: '/ws' });
  wss.on('connection', attachWorker);

  server.listen(PORT, '0.0.0.0', () => {
    console.log(`[server] listening on port ${PORT}`);
  });
}

main().catch((err) => {
  console.error('[server] fatal', err);
  process.exit(1);
});
