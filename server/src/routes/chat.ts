import { Router, Request, Response } from 'express';
import { ObjectId } from 'mongodb';
import {
  activeTurnFor,
  conversations,
  isWorkerConnected,
  startTurn,
  subscribe,
  type BoothEntry,
  type ConversationDoc,
} from '../relay.js';

export const chatRouter = Router();

const MAX_MESSAGE_CHARS = 8000;
// Number of most recent messages sent to the worker as context
const HISTORY_WINDOW = 30;

function parseId(raw: string): ObjectId | null {
  return /^[a-f0-9]{24}$/i.test(raw) ? new ObjectId(raw) : null;
}

// ── POST /api/chat ─────────────────────────────────────────────────────────
// Body: { conversationId?: string, message: string }
// Returns: { conversationId: string, turnId: string }
chatRouter.post('/chat', async (req: Request, res: Response) => {
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
    const oid = parseId(conversationId);
    convo = oid ? await col.findOne({ _id: oid }) : null;
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
    convo = { _id: new ObjectId(), createdAt: now, updatedAt: now, messages: [userMessage], results: [] };
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
  const oid = parseId(req.params.id);
  const convo = oid ? await conversations().findOne({ _id: oid }) : null;
  if (!convo) {
    res.status(404).json({ error: 'not found' });
    return;
  }
  res.json({
    id: convo._id.toHexString(),
    messages: convo.messages.map(({ role, content }) => ({ role, content })),
    results: convo.results,
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

// ── POST /api/conversations/:id/generate-title ─────────────────────────────
// Uses GPT-4o-mini to produce a concise 4–5 word title from the first message.
chatRouter.post('/conversations/:id/generate-title', async (req: Request, res: Response) => {
  const oid = parseId(req.params.id);
  const convo = oid ? await conversations().findOne({ _id: oid }) : null;
  if (!convo) {
    res.status(404).json({ error: 'not found' });
    return;
  }

  const firstUserMsg = convo.messages.find((m) => m.role === 'user')?.content ?? '';
  const fallback = firstUserMsg.split(/\s+/).slice(0, 5).join(' ') || 'New conversation';

  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
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
    res.json({ title: raw || fallback });
  } catch {
    res.json({ title: fallback });
  }
});

// ── GET /api/conversations/:id/export ──────────────────────────────────────
// Returns the conversation's results table as a CSV file
chatRouter.get('/conversations/:id/export', async (req: Request, res: Response) => {
  const oid = parseId(req.params.id);
  const convo = oid ? await conversations().findOne({ _id: oid }) : null;
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
