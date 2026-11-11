import { ObjectId } from 'mongodb';
import { getDb } from './db.js';
import {
  conversations,
  isWorkerConnected,
  sendToWorker,
  writeConversation,
  type ChatMessage,
  type ResultRow,
} from './relay.js';

// ── Background crawl jobs ──────────────────────────────────────────────────
// The worker extracts a job's URLs in batches outside chat turns. The server keeps
// the job record (URLs, cursor, status) so jobs survive worker and server restarts.

export type JobStatus = 'queued' | 'running' | 'paused' | 'done' | 'cancelled' | 'failed';

const ACTIVE: JobStatus[] = ['queued', 'running', 'paused'];
const RUNNABLE: JobStatus[] = ['queued', 'running'];
const MAX_FAILED = 200;
// Failed pages included in the chat context for the agent
const CONTEXT_FAILED = 50;

interface FailedPage {
  url: string;
  error: string;
}

export interface JobDoc {
  _id: ObjectId;
  jobId: string;
  conversationId: ObjectId;
  userId: string;
  status: JobStatus;
  fields: string[];
  keyFields: string[];
  instructions: string;
  urlField: string;
  /** Pages built from a results column; their rows fill the rows sharing its value. */
  urlTemplate?: string;
  urls: string[];
  cursor: number;
  rowsAdded: number;
  failed: FailedPage[];
  failedCount: number;
  error?: string;
  /** The finish message added to the conversation. */
  message?: string;
  createdAt: Date;
  updatedAt: Date;
  finishedAt?: Date;
}

/** What the browser sees: progress without the URL list. */
export interface JobSummary {
  jobId: string;
  status: JobStatus;
  fields: string[];
  total: number;
  cursor: number;
  rowsAdded: number;
  failedCount: number;
  error?: string;
  message?: string;
  createdAt: number;
  finishedAt?: number;
}

interface JobSpecPayload {
  jobId: string;
  conversationId: string;
  fields: string[];
  keyFields: string[];
  instructions: string;
  urlField: string;
  urlTemplate?: string;
  urls: string[];
}

interface JobProgressPayload {
  cursor: number;
  rowsAdded: number;
  failed: FailedPage[];
  results: ResultRow[];
}

interface JobFinishedPayload {
  status: 'done' | 'cancelled' | 'failed';
  error?: string;
}

export function jobs() {
  return getDb().collection<JobDoc>('jobs');
}

export async function ensureJobIndexes(): Promise<void> {
  await jobs().createIndex({ jobId: 1 }, { unique: true });
  await jobs().createIndex({ conversationId: 1, createdAt: -1 });
  await jobs().createIndex({ status: 1 });
}

export function summarize(job: JobDoc): JobSummary {
  return {
    jobId: job.jobId,
    status: job.status,
    fields: job.fields,
    total: job.urls.length,
    cursor: job.cursor,
    rowsAdded: job.rowsAdded,
    failedCount: job.failedCount,
    ...(job.error ? { error: job.error } : {}),
    ...(job.message ? { message: job.message } : {}),
    createdAt: job.createdAt.getTime(),
    ...(job.finishedAt ? { finishedAt: job.finishedAt.getTime() } : {}),
  };
}

/** The job info sent with each chat turn, so the agent knows about it. */
export function contextFor(job: JobDoc) {
  return { ...summarize(job), failed: job.failed.slice(0, CONTEXT_FAILED) };
}

export function latestJob(conversationId: ObjectId): Promise<JobDoc | null> {
  return jobs().findOne({ conversationId }, { sort: { createdAt: -1 } });
}

function spec(job: JobDoc): JobSpecPayload {
  return {
    jobId: job.jobId,
    conversationId: job.conversationId.toHexString(),
    fields: job.fields,
    keyFields: job.keyFields,
    instructions: job.instructions,
    urlField: job.urlField,
    urlTemplate: job.urlTemplate ?? '',
    urls: job.urls,
  };
}

async function sendRun(job: JobDoc): Promise<void> {
  const convo = await conversations().findOne({ _id: job.conversationId }, { projection: { results: 1 } });
  sendToWorker({ type: 'job_run', job: spec(job), cursor: job.cursor, results: convo?.results ?? [] });
}

/** Re-send every unfinished job when the worker (re)connects. */
export async function dispatchJobs(): Promise<void> {
  const pending = await jobs().find({ status: { $in: RUNNABLE } }, { sort: { createdAt: 1 } }).toArray();
  for (const job of pending) await sendRun(job);
  if (pending.length) console.log(`[jobs] dispatched ${pending.length} unfinished job(s) to the worker`);
}

// ── Worker frames ──────────────────────────────────────────────────────────

export async function handleJobFrame(type: string, jobId: string, payload: unknown): Promise<void> {
  if (type === 'job_created') return created(payload as JobSpecPayload);
  const job = await jobs().findOne({ jobId });
  if (!job) return;
  if (type === 'job_progress') return progress(job, payload as JobProgressPayload);
  if (type === 'job_finished') return finished(job, payload as JobFinishedPayload);
}

async function created(payload: JobSpecPayload): Promise<void> {
  const conversationId = ObjectId.isValid(payload.conversationId) ? new ObjectId(payload.conversationId) : null;
  const convo = conversationId ? await conversations().findOne({ _id: conversationId }, { projection: { userId: 1 } }) : null;
  if (!convo) {
    sendToWorker({ type: 'job_cancel', jobId: payload.jobId });
    return;
  }
  const now = new Date();
  await jobs().insertOne({
    _id: new ObjectId(),
    jobId: payload.jobId,
    conversationId: convo._id,
    userId: convo.userId,
    status: 'queued',
    fields: payload.fields,
    keyFields: payload.keyFields,
    instructions: payload.instructions,
    urlField: payload.urlField,
    ...(payload.urlTemplate ? { urlTemplate: payload.urlTemplate } : {}),
    urls: payload.urls,
    cursor: 0,
    rowsAdded: 0,
    failed: [],
    failedCount: 0,
    createdAt: now,
    updatedAt: now,
  });
  console.log(`[jobs] ${payload.jobId} created: ${payload.urls.length} pages`);
}

async function progress(job: JobDoc, payload: JobProgressPayload): Promise<void> {
  const failed = Array.isArray(payload.failed) ? payload.failed : [];
  const now = new Date();
  await jobs().updateOne(
    { _id: job._id },
    {
      $set: { cursor: payload.cursor, rowsAdded: payload.rowsAdded, updatedAt: now },
      $inc: { failedCount: failed.length },
      $push: { failed: { $each: failed, $slice: MAX_FAILED } },
    },
  );
  await jobs().updateOne({ _id: job._id, status: 'queued' }, { $set: { status: 'running' } });
  if (Array.isArray(payload.results)) {
    await writeConversation(job.conversationId, () =>
      conversations().updateOne({ _id: job.conversationId }, { $set: { results: payload.results, updatedAt: now } }),
    );
  }
}

async function finished(job: JobDoc, payload: JobFinishedPayload): Promise<void> {
  await finish(job, payload.status, payload.error);
}

/** Mark the job finished once and add the finish message to the conversation. */
async function finish(job: JobDoc, status: 'done' | 'cancelled' | 'failed', error?: string): Promise<void> {
  await writeConversation(job.conversationId, async () => {
    const current = await jobs().findOne({ _id: job._id, status: { $in: ACTIVE } });
    if (!current) return;
    const convo = await conversations().findOne({ _id: job.conversationId }, { projection: { results: 1 } });
    const content = finishMessage({ ...current, status, error }, convo?.results.length ?? 0);
    const now = new Date();
    const updated = await jobs().findOneAndUpdate(
      { _id: job._id, status: { $in: ACTIVE } },
      { $set: { status, finishedAt: now, updatedAt: now, message: content, ...(error ? { error } : {}) } },
    );
    if (!updated) return;
    console.log(`[jobs] ${job.jobId} ${status} at ${current.cursor}/${current.urls.length}`);
    const message: ChatMessage = { role: 'assistant', content, at: now };
    await conversations().updateOne(
      { _id: job.conversationId },
      { $push: { messages: message }, $set: { updatedAt: now } },
    );
  });
}

function count(n: number, word: string): string {
  return `${n.toLocaleString('en-US')} ${word}${n === 1 ? '' : 's'}`;
}

function finishMessage(job: JobDoc, rows: number): string {
  const total = job.urls.length;
  const failed = job.failedCount
    ? ` ${count(job.failedCount, 'page')} couldn't be read — ask me if you'd like me to try ${job.failedCount === 1 ? 'it' : 'them'} again.`
    : '';
  if (job.status === 'done') {
    return `🎉 Background job finished! I read ${count(total, 'page')} and the results table now has ${count(rows, 'row')}.${failed}`;
  }
  if (job.status === 'cancelled') {
    return `🛑 Background job cancelled after ${job.cursor.toLocaleString('en-US')} of ${count(total, 'page')}. The ${count(rows, 'row')} collected so far are in the results table.`;
  }
  return `⚠️ The background job stopped after ${job.cursor.toLocaleString('en-US')} of ${count(total, 'page')}${job.error ? `: ${job.error}` : ''}. The ${count(rows, 'row')} collected so far are in the results table.`;
}

// ── User actions ───────────────────────────────────────────────────────────

export type JobAction = 'pause' | 'resume' | 'cancel';

/** Apply a user action; returns the updated job, or null if it doesn't apply. */
export async function applyAction(job: JobDoc, action: JobAction): Promise<JobDoc | null> {
  const now = new Date();
  if (action === 'pause') {
    const updated = await jobs().findOneAndUpdate(
      { _id: job._id, status: { $in: RUNNABLE } },
      { $set: { status: 'paused', updatedAt: now } },
      { returnDocument: 'after' },
    );
    if (updated) sendToWorker({ type: 'job_pause', jobId: job.jobId });
    return updated;
  }
  if (action === 'resume') {
    const updated = await jobs().findOneAndUpdate(
      { _id: job._id, status: 'paused' },
      { $set: { status: 'queued', updatedAt: now } },
      { returnDocument: 'after' },
    );
    if (updated && isWorkerConnected()) await sendRun(updated);
    return updated;
  }
  if (!ACTIVE.includes(job.status)) return null;
  sendToWorker({ type: 'job_cancel', jobId: job.jobId });
  await finish(job, 'cancelled');
  return jobs().findOne({ _id: job._id });
}
