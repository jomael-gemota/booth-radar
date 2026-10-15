import { Router, Request, Response } from 'express';
import { ObjectId } from 'mongodb';
import {
  activeTurnFor,
  conversations,
  files,
  isWorkerConnected,
  startTurn,
  subscribe,
  type BoothEntry,
  type ConversationDoc,
} from '../relay.js';
import { requireAuth } from '../middleware/requireAuth.js';

export const chatRouter = Router();

// All chat routes require authentication
chatRouter.use(requireAuth);

const MAX_MESSAGE_CHARS = 8000;
// Number of most recent messages sent to the worker as context
const HISTORY_WINDOW = 30;

function parseId(raw: string): ObjectId | null {
  return /^[a-f0-9]{24}$/i.test(raw) ? new ObjectId(raw) : null;
}

/** Find a conversation by ID scoped to the authenticated user. */
async function findOwnedConv(id: string, userId: string): Promise<ConversationDoc | null> {
  const oid = parseId(id);
  if (!oid) return null;
  return conversations().findOne({ _id: oid, userId });
}

// ── GET /api/conversations ─────────────────────────────────────────────────
// Returns a summary list of the current user's conversations, newest first.
chatRouter.get('/conversations', async (req: Request, res: Response) => {
  const userId = req.user!.id;
  const docs = await conversations()
    .find(
      { userId },
      {
        projection: { messages: 0, results: 0 },
        sort: { updatedAt: -1 },
        limit: 200,
      },
    )
    .toArray();

  res.json({
    conversations: docs.map((c) => ({
      id:        c._id.toHexString(),
      title:     c.title   ?? '',
      preview:   c.preview ?? '',
      createdAt: c.createdAt.getTime(),
      updatedAt: c.updatedAt.getTime(),
    })),
  });
});

// ── POST /api/chat ─────────────────────────────────────────────────────────
// Body: { conversationId?: string, message: string }
// Returns: { conversationId: string, turnId: string }
chatRouter.post('/chat', async (req: Request, res: Response) => {
  const userId = req.user!.id;
  const { conversationId, message } = req.body as { conversationId?: string; message?: string };

  const text = typeof message === 'string' ? message.trim() : '';
  if (!text) {
    res.status(400).json({ error: 'message is required' });
    return;
  }
  if (text.length > MAX_MESSAGE_CHARS) {
    res.status(400).json({ error: `message is too long (max ${MAX_MESSAGE_CHARS} characters)` });
    return;
  }

  if (!isWorkerConnected()) {
    res.status(503).json({ error: 'The Booth Radar worker is offline. Please try again once it reconnects.' });
    return;
  }

  const col = conversations();
  const now = new Date();
  let convo: ConversationDoc | null = null;

  if (conversationId) {
    convo = await findOwnedConv(conversationId, userId);
    if (!convo) {
      res.status(404).json({ error: 'conversation not found' });
      return;
    }
    if (activeTurnFor(conversationId)) {
      res.status(409).json({ error: 'Booth Radar is still working on your previous message.' });
      return;
    }
  }

  const userMessage = { role: 'user' as const, content: text, at: now };

  if (convo) {
    await col.updateOne(
      { _id: convo._id },
      { $push: { messages: userMessage }, $set: { updatedAt: now } },
    );
    convo.messages.push(userMessage);
  } else {
    // New conversation — store a preview from the first message
    const preview = text.length > 120 ? text.slice(0, 120) + '…' : text;
    convo = {
      _id: new ObjectId(),
      userId,
      preview,
      createdAt: now,
      updatedAt: now,
      messages:  [userMessage],
      results:   [],
    };
    await col.insertOne(convo);
  }

  const turnId = new ObjectId().toHexString();
  const history = convo.messages
    .slice(-HISTORY_WINDOW)
    .map(({ role, content }) => ({ role, content }));

  try {
    startTurn(turnId, convo._id, history, convo.results);
  } catch {
    res.status(503).json({ error: 'The Booth Radar worker is offline. Please try again once it reconnects.' });
    return;
  }

  res.json({ conversationId: convo._id.toHexString(), turnId });
});

// ── GET /api/conversations/:id ─────────────────────────────────────────────
chatRouter.get('/conversations/:id', async (req: Request, res: Response) => {
  const convo = await findOwnedConv(req.params.id, req.user!.id);
  if (!convo) {
    res.status(404).json({ error: 'not found' });
    return;
  }
  res.json({
    id:          convo._id.toHexString(),
    messages:    convo.messages.map(({ role, content, attachments, duration }) => ({ role, content, attachments, duration })),
    results:     convo.results,
    activeTurnId: activeTurnFor(req.params.id) ?? null,
  });
});

// ── GET /api/stream/:turnId ────────────────────────────────────────────────
// Server-Sent Events — relays worker events for one assistant turn
chatRouter.get('/stream/:turnId', (req: Request, res: Response) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();

  if (!subscribe(req.params.turnId, res)) {
    res.write(`event: error\ndata: ${JSON.stringify({ message: 'This request has expired.' })}\n\n`);
    res.end();
    return;
  }

  // Keep-alive ping every 20 s
  const ping = setInterval(() => res.write(': ping\n\n'), 20_000);
  res.on('close', () => clearInterval(ping));
});

// ── PATCH /api/conversations/:id ──────────────────────────────────────────
// Body: { title: string }  — lets the user rename a conversation
chatRouter.patch('/conversations/:id', async (req: Request, res: Response) => {
  const userId = req.user!.id;
  const { title } = req.body as { title?: unknown };

  if (typeof title !== 'string' || !title.trim()) {
    res.status(400).json({ error: 'title is required' });
    return;
  }

  const convo = await findOwnedConv(req.params.id, userId);
  if (!convo) {
    res.status(404).json({ error: 'not found' });
    return;
  }

  const trimmed = title.trim().slice(0, 100);
  await conversations().updateOne(
    { _id: convo._id },
    { $set: { title: trimmed, updatedAt: new Date() } },
  );

  res.json({ title: trimmed });
});

// ── POST /api/conversations/:id/generate-title ─────────────────────────────
// Uses GPT-4o-mini to produce a concise 4–5 word title from the first message.
chatRouter.post('/conversations/:id/generate-title', async (req: Request, res: Response) => {
  const convo = await findOwnedConv(req.params.id, req.user!.id);
  if (!convo) {
    res.status(404).json({ error: 'not found' });
    return;
  }

  const firstUserMsg = convo.messages.find((m) => m.role === 'user')?.content ?? '';
  const fallback = firstUserMsg.split(/\s+/).slice(0, 5).join(' ') || 'New conversation';

  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    await conversations().updateOne({ _id: convo._id }, { $set: { title: fallback } });
    res.json({ title: fallback });
    return;
  }

  try {
    const response = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({
        model: 'gpt-4o-mini',
        temperature: 0.4,
        max_tokens: 20,
        messages: [
          {
            role: 'system',
            content:
              'You generate very short conversation titles. ' +
              'Return ONLY the title — 4 to 5 words, no quotes, no punctuation at the end, title-case.',
          },
          {
            role: 'user',
            content: `First message: "${firstUserMsg.slice(0, 300)}"`,
          },
        ],
      }),
    });

    interface OAIRes { choices: Array<{ message: { content: string } }> }
    const data = (await response.json()) as OAIRes;
    const raw = data.choices[0]?.message?.content?.trim() ?? '';
    const title = raw || fallback;
    // Persist title to MongoDB
    await conversations().updateOne({ _id: convo._id }, { $set: { title } });
    res.json({ title });
  } catch {
    await conversations().updateOne({ _id: convo._id }, { $set: { title: fallback } });
    res.json({ title: fallback });
  }
});

// ── GET /api/conversations/:id/export ──────────────────────────────────────
// Returns the conversation's results table as a CSV file
chatRouter.get('/conversations/:id/export', async (req: Request, res: Response) => {
  const convo = await findOwnedConv(req.params.id, req.user!.id);
  if (!convo) {
    res.status(404).json({ error: 'not found' });
    return;
  }
  if (!convo.results.length) {
    res.status(409).json({ error: 'no results to export yet' });
    return;
  }

  const filename = `booth-radar-${req.params.id}.csv`;
  res.setHeader('Content-Type', 'text/csv');
  res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
  res.send(buildCsv(convo.results));
});

// ── GET /api/files/:id ─────────────────────────────────────────────────────
// Downloads a file the agent produced during a turn
chatRouter.get('/files/:id', async (req: Request, res: Response) => {
  const userId = req.user!.id;
  const oid    = parseId(req.params.id);
  const file   = oid ? await files().findOne({ _id: oid }) : null;
  if (!file) {
    res.status(404).json({ error: 'not found' });
    return;
  }

  // Verify the file belongs to a conversation the user owns
  const ownerConvo = await conversations().findOne({ _id: file.conversationId, userId });
  if (!ownerConvo) {
    res.status(403).json({ error: 'Forbidden' });
    return;
  }

  const asciiName = file.name.replace(/[^\w.\- ]/g, '_');
  res.setHeader('Content-Type', file.mime);
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader(
    'Content-Disposition',
    `attachment; filename="${asciiName}"; filename*=UTF-8''${encodeURIComponent(file.name)}`,
  );
  res.send(Buffer.from(file.data.buffer));
});

// ── Helpers ────────────────────────────────────────────────────────────────

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
