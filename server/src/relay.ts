import type { Response } from 'express';
import { WebSocket } from 'ws';
import { Binary, ObjectId } from 'mongodb';
import { getDb } from './db.js';
import { dispatchJobs, handleJobFrame } from './jobs.js';

// ── Shared types ───────────────────────────────────────────────────────────

/**
 * One results-table row. The worker always sends every column, so the column
 * order is the key order of the first row (legacy rows are `{ brand, booth }`).
 */
export type ResultRow = Record<string, string>;

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
  results: ResultRow[];
}

type WorkerEventType = 'thinking' | 'token' | 'status' | 'results' | 'file' | 'done' | 'error' | 'cancelled';

interface WorkerMessage {
  turnId: string;
  type: WorkerEventType;
  payload: unknown;
}

const JOB_FRAMES = new Set(['job_created', 'job_progress', 'job_finished']);

interface WorkerJobMessage {
  jobId: string;
  type: string;
  payload: unknown;
}

interface TurnEvent {
  /** SSE event id, so a reconnecting browser only gets the events it missed. */
  id: number;
  type: WorkerEventType;
  frame: string;
}

interface Turn {
  conversationId: ObjectId;
  events: TurnEvent[];
  lastEventId: number;
  clients: Set<Response>;
  finished: boolean;
  /** Accumulates thinking chunks and status lines in-memory so they can be persisted on done. */
  thinkingBuffer: string;
  /** Whether the buffer's last line is a progress line, which the next one replaces. */
  lastLineProgress: boolean;
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

/** Send a control message to the worker; false when it isn't connected. */
export function sendToWorker(message: Record<string, unknown>): boolean {
  if (!workerSocket || !isWorkerConnected()) return false;
  workerSocket.send(JSON.stringify(message));
  return true;
}

// A turn and a background job both save full results snapshots of the same
// conversation, so its writes run one at a time in arrival order.
const conversationWrites = new Map<string, Promise<unknown>>();

export function writeConversation(conversationId: ObjectId, write: () => Promise<unknown>): Promise<unknown> {
  const key = conversationId.toHexString();
  const next = (conversationWrites.get(key) ?? Promise.resolve()).catch(() => undefined).then(write);
  conversationWrites.set(key, next);
  next
    .catch(() => undefined)
    .finally(() => {
      if (conversationWrites.get(key) === next) conversationWrites.delete(key);
    });
  return next;
}

// Job frames are handled one at a time so a job's record exists before its progress.
let jobFrames: Promise<unknown> = Promise.resolve();

export function activeTurnFor(conversationId: string): string | undefined {
  return activeTurns.get(conversationId);
}

// ── Turn lifecycle ─────────────────────────────────────────────────────────

export function startTurn(
  turnId: string,
  conversationId: ObjectId,
  messages: Pick<ChatMessage, 'role' | 'content'>[],
  results: ResultRow[],
  job: unknown = null,
): void {
  if (!workerSocket || !isWorkerConnected()) {
    throw new Error('Worker is not connected');
  }
  turns.set(turnId, {
    conversationId,
    events: [],
    lastEventId: 0,
    clients: new Set(),
    finished: false,
    thinkingBuffer: '',
    lastLineProgress: false,
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
    job,
  }));
}

function broadcast(turnId: string, turn: Turn, type: WorkerEventType, payload: unknown): void {
  const id = ++turn.lastEventId;
  const frame = `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(payload)}\n\n`;
  // Each results frame is a full snapshot: late subscribers only need the latest.
  if (type === 'results') turn.events = turn.events.filter((e) => e.type !== 'results');
  turn.events.push({ id, type, frame });
  for (const res of turn.clients) res.write(frame);

  if (type === 'done' || type === 'error' || type === 'cancelled') {
    turn.finished = true;
    activeTurns.delete(turn.conversationId.toHexString());
    for (const res of turn.clients) res.end();
    turn.clients.clear();
    setTimeout(() => turns.delete(turnId), TURN_RETENTION_MS);
  }
}

/** Attach an SSE response to a turn, replaying the events emitted after event id *after*. */
export function subscribe(turnId: string, res: Response, after = 0): boolean {
  const turn = turns.get(turnId);
  if (!turn) return false;

  for (const event of turn.events) if (event.id > after) res.write(event.frame);
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
  if (type === 'cancelled') return; // turn was aborted — nothing to save
  const col = conversations();
  if (type === 'thinking') {
    // Accumulate in-memory only — no DB write per chunk
    turn.thinkingBuffer += (payload as { text: string }).text;
    turn.lastLineProgress = false;
  } else if (type === 'status') {
    // Status lines are shown inside the reasoning block, one per line; a progress
    // line replaces the progress line before it.
    const { text, progress } = payload as { text: string; progress?: boolean };
    let buffer = turn.thinkingBuffer;
    if (progress && turn.lastLineProgress) buffer = buffer.slice(0, buffer.lastIndexOf('\n', buffer.length - 2) + 1);
    const separator = buffer && !buffer.endsWith('\n') ? '\n' : '';
    turn.thinkingBuffer = `${buffer}${separator}${text}\n`;
    turn.lastLineProgress = Boolean(progress);
  } else if (type === 'results') {
    const { results } = payload as { results: ResultRow[] };
    await writeConversation(turn.conversationId, () =>
      col.updateOne({ _id: turn.conversationId }, { $set: { results, updatedAt: new Date() } }),
    );
  } else if (type === 'done') {
    const { reply, results } = payload as { reply: string; results: ResultRow[] };
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
    await writeConversation(turn.conversationId, () =>
      col.updateOne(
        { _id: turn.conversationId },
        {
          $set:  { results, updatedAt: now },
          $push: { messages: { $each: newMessages } } as never,
        },
      ),
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

// ── Cancel API ────────────────────────────────────────────────────────────

/** Returns the conversationId hex string for an active turn, or null. */
export function getTurnConversationId(turnId: string): string | null {
  return turns.get(turnId)?.conversationId.toHexString() ?? null;
}

/**
 * Immediately closes all SSE clients for the turn and signals the worker to
 * stop. Returns false if the turn is unknown or already finished.
 */
export function cancelTurn(turnId: string): boolean {
  const turn = turns.get(turnId);
  if (!turn || turn.finished) return false;
  // Signal the worker process to stop its thread
  if (workerSocket && workerSocket.readyState === WebSocket.OPEN) {
    workerSocket.send(JSON.stringify({ type: 'cancel', turnId }));
  }
  // Close SSE clients immediately
  broadcast(turnId, turn, 'cancelled', {});
  return true;
}

// ── Worker socket ──────────────────────────────────────────────────────────

export function attachWorker(ws: WebSocket): void {
  console.log('[ws] worker connected');
  workerSocket = ws;
  dispatchJobs().catch((err) => console.error('[jobs] failed to dispatch unfinished jobs', err));

  ws.on('message', (raw) => {
    let msg: WorkerMessage;
    try {
      msg = JSON.parse(raw.toString()) as WorkerMessage;
    } catch (err) {
      console.error('[ws] failed to parse worker message', err);
      return;
    }

    if (JOB_FRAMES.has(msg.type)) {
      const { type, jobId, payload } = msg as unknown as WorkerJobMessage;
      jobFrames = jobFrames
        .then(() => handleJobFrame(type, jobId, payload))
        .catch((err) => console.error(`[jobs] failed to handle ${type}`, err));
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
