import type { Response } from 'express';
import { WebSocket } from 'ws';
import { Binary, ObjectId } from 'mongodb';
import { getDb } from './db.js';

// ── Shared types ───────────────────────────────────────────────────────────

export interface BoothEntry {
  brand: string;
  booth: string;
}

export interface Attachment {
  id: string;
  name: string;
  size: number;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'thinking';
  content: string;
  at: Date;
  attachments?: Attachment[];
  /** Milliseconds from worker dispatch to turn completion (assistant messages only). */
  duration?: number;
}

export interface FileDoc {
  _id: ObjectId;
  conversationId: ObjectId;
  name: string;
  mime: string;
  size: number;
  data: Binary;
  createdAt: Date;
}

interface WorkerFilePayload {
  name: string;
  mime: string;
  data: string;
}

const MAX_FILE_BYTES = Number(process.env.MAX_FILE_BYTES ?? 10 * 1024 * 1024);

export interface ConversationDoc {
  _id: ObjectId;
  userId: string;    // Google profile ID of the owner
  title?: string;    // AI-generated title (saved by generate-title)
  preview?: string;  // First user message, truncated
  createdAt: Date;
  updatedAt: Date;
  messages: ChatMessage[];
  results: BoothEntry[];
}

type WorkerEventType = 'thinking' | 'token' | 'status' | 'results' | 'file' | 'done' | 'error';

interface WorkerMessage {
  turnId: string;
  type: WorkerEventType;
  payload: unknown;
}

interface Turn {
  conversationId: ObjectId;
  events: string[];
  clients: Set<Response>;
  finished: boolean;
  /** Accumulates thinking chunks in-memory so the full text can be persisted on done. */
  thinkingBuffer: string;
  attachments: Attachment[];
  /** File inserts still in flight; `done` waits for them before saving the reply. */
  pendingFiles: Promise<unknown>[];
  /** Unix ms when startTurn() was called — used to compute response duration. */
  startedAt: number;
}

// How long a finished turn's events stay available for late SSE subscribers
const TURN_RETENTION_MS = 60_000;

const turns = new Map<string, Turn>();
// conversationId → turnId of the turn currently running
const activeTurns = new Map<string, string>();

let workerSocket: WebSocket | null = null;

export function conversations() {
  return getDb().collection<ConversationDoc>('conversations');
}

export function files() {
  return getDb().collection<FileDoc>('files');
}

export function isWorkerConnected(): boolean {
  return workerSocket !== null && workerSocket.readyState === WebSocket.OPEN;
}

export function activeTurnFor(conversationId: string): string | undefined {
  return activeTurns.get(conversationId);
}

// ── Turn lifecycle ─────────────────────────────────────────────────────────

export function startTurn(
  turnId: string,
  conversationId: ObjectId,
  messages: Pick<ChatMessage, 'role' | 'content'>[],
  results: BoothEntry[],
): void {
  if (!workerSocket || !isWorkerConnected()) {
    throw new Error('Worker is not connected');
  }
  turns.set(turnId, {
    conversationId,
    events: [],
    clients: new Set(),
    finished: false,
    thinkingBuffer: '',
    attachments: [],
    pendingFiles: [],
    startedAt: Date.now(),
  });
  activeTurns.set(conversationId.toHexString(), turnId);
  workerSocket.send(JSON.stringify({
    type: 'chat',
    turnId,
    conversationId: conversationId.toHexString(),
    messages,
    results,
  }));
}

function broadcast(turnId: string, turn: Turn, type: WorkerEventType, payload: unknown): void {
  const frame = `event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`;
  turn.events.push(frame);
  for (const res of turn.clients) res.write(frame);

  if (type === 'done' || type === 'error') {
    turn.finished = true;
    activeTurns.delete(turn.conversationId.toHexString());
    for (const res of turn.clients) res.end();
    turn.clients.clear();
    setTimeout(() => turns.delete(turnId), TURN_RETENTION_MS);
  }
}

/** Attach an SSE response to a turn, replaying events emitted so far. */
export function subscribe(turnId: string, res: Response): boolean {
  const turn = turns.get(turnId);
  if (!turn) return false;

  for (const frame of turn.events) res.write(frame);
  if (turn.finished) {
    res.end();
    return true;
  }

  turn.clients.add(res);
  res.on('close', () => turn.clients.delete(res));
  return true;
}

// ── Persistence ────────────────────────────────────────────────────────────

async function persist(turn: Turn, type: WorkerEventType, payload: unknown): Promise<void> {
  const col = conversations();
  if (type === 'thinking') {
    // Accumulate in-memory only — no DB write per chunk
    turn.thinkingBuffer += (payload as { text: string }).text;
  } else if (type === 'results') {
    const { results } = payload as { results: BoothEntry[] };
    await col.updateOne({ _id: turn.conversationId }, { $set: { results, updatedAt: new Date() } });
  } else if (type === 'done') {
    const { reply, results } = payload as { reply: string; results: BoothEntry[] };
    await Promise.allSettled(turn.pendingFiles);
    const now = new Date();
    const duration = Date.now() - turn.startedAt;
    // Persist thinking block first (if any), then the assistant reply
    const newMessages: ChatMessage[] = [];
    if (turn.thinkingBuffer.trim()) {
      newMessages.push({ role: 'thinking', content: turn.thinkingBuffer, at: now });
    }
    newMessages.push({
      role: 'assistant',
      content: reply,
      at: now,
      duration,
      ...(turn.attachments.length ? { attachments: turn.attachments } : {}),
    });
    await col.updateOne(
      { _id: turn.conversationId },
      {
        $set:  { results, updatedAt: now },
        $push: { messages: { $each: newMessages } } as never,
      },
    );
  }
}

/** Store a worker file and return the metadata sent to the browser (never the bytes). */
function acceptFile(turn: Turn, payload: WorkerFilePayload): Attachment | null {
  const name = typeof payload?.name === 'string' ? payload.name.trim() : '';
  if (!name || typeof payload.data !== 'string') return null;
  const data = Buffer.from(payload.data, 'base64');
  if (data.length > MAX_FILE_BYTES) {
    console.warn(`[files] rejected ${name}: ${data.length} bytes > ${MAX_FILE_BYTES}`);
    return null;
  }

  const _id = new ObjectId();
  const attachment: Attachment = { id: _id.toHexString(), name, size: data.length };
  turn.attachments.push(attachment);
  turn.pendingFiles.push(
    files()
      .insertOne({
        _id,
        conversationId: turn.conversationId,
        name,
        mime: typeof payload.mime === 'string' ? payload.mime : 'application/octet-stream',
        size: data.length,
        data: new Binary(data),
        createdAt: new Date(),
      })
      .catch((err) => {
        console.error(`[db] failed to store file ${name}`, err);
        turn.attachments = turn.attachments.filter((a) => a.id !== attachment.id);
      }),
  );
  return attachment;
}

// ── Worker socket ──────────────────────────────────────────────────────────

export function attachWorker(ws: WebSocket): void {
  console.log('[ws] worker connected');
  workerSocket = ws;

  ws.on('message', (raw) => {
    let msg: WorkerMessage;
    try {
      msg = JSON.parse(raw.toString()) as WorkerMessage;
    } catch (err) {
      console.error('[ws] failed to parse worker message', err);
      return;
    }

    const turn = turns.get(msg.turnId);
    if (!turn || turn.finished) return;

    if (msg.type === 'file') {
      const attachment = acceptFile(turn, msg.payload as WorkerFilePayload);
      if (attachment) broadcast(msg.turnId, turn, 'file', attachment);
      return;
    }

    persist(turn, msg.type, msg.payload).catch((err) =>
      console.error('[db] failed to persist turn event', err),
    );
    broadcast(msg.turnId, turn, msg.type, msg.payload);
  });

  const detach = () => {
    if (workerSocket !== ws) return;
    console.log('[ws] worker disconnected');
    workerSocket = null;
    for (const [turnId, turn] of turns) {
      if (!turn.finished) {
        broadcast(turnId, turn, 'error', {
          message: 'The worker disconnected while handling your message. Please try again.',
        });
      }
    }
  };

  ws.on('close', detach);
  ws.on('error', (err) => {
    console.error('[ws] worker error', err);
    detach();
  });
}
