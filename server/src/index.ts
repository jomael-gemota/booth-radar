import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import session from 'express-session';
import MongoStore from 'connect-mongo';
import passport from 'passport';
import { WebSocketServer } from 'ws';
import http from 'http';
import path from 'path';
import fs from 'fs';
import { connectDb } from './db.js';
import { attachWorker, isWorkerConnected } from './relay.js';
import { chatRouter } from './routes/chat.js';
import { greetingRouter } from './routes/greeting.js';
import { authRouter } from './routes/auth.js';
import { configurePassport } from './auth.js';

const PORT = Number(process.env.PORT ?? 3001);
const CLIENT_ORIGIN = process.env.CLIENT_ORIGIN ?? 'http://localhost:5173';
const IS_PROD = process.env.NODE_ENV === 'production';

async function main() {
  await connectDb();

  const MONGODB_URI = process.env.MONGODB_URI ?? 'mongodb://localhost:27017';

  const app = express();

  // Trust Railway's reverse proxy so that secure cookies and req.protocol
  // work correctly when the app sits behind an HTTPS load balancer.
  app.set('trust proxy', 1);

  // In dev (no client/dist), allow Vite's dev server origin for CORS
  if (!IS_PROD) {
    app.use(cors({ origin: CLIENT_ORIGIN, credentials: true }));
  }

  // ── Session (stored in MongoDB) ────────────────────────────────────────
  app.use(
    session({
      secret:            process.env.SESSION_SECRET ?? 'dev-secret-change-me',
      resave:            false,
      saveUninitialized: false,
      store: MongoStore.create({
        mongoUrl:   MONGODB_URI,
        dbName:     process.env.MONGODB_DB ?? 'booth-radar',
        collectionName: 'sessions',
        ttl:        7 * 24 * 60 * 60, // 7 days in seconds
      }),
      cookie: {
        httpOnly: true,
        secure:   IS_PROD,
        maxAge:   7 * 24 * 60 * 60 * 1000,
        // Must be 'lax' (not 'strict') so the session cookie is sent when
        // Google redirects the browser back to the callback URL — that
        // redirect is a cross-site top-level navigation and 'strict' blocks it.
        sameSite: 'lax',
      },
    }),
  );

  // ── Passport (Google OAuth) ────────────────────────────────────────────
  configurePassport();
  app.use(passport.initialize());
  app.use(passport.session());

  app.use(express.json());

  app.use('/api', authRouter);
  app.use('/api', chatRouter);
  app.use('/api', greetingRouter);

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
