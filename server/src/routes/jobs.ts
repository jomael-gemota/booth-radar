import { Router, Request, Response } from 'express';
import { ObjectId } from 'mongodb';
import { conversations } from '../relay.js';
import { applyAction, jobs, latestJob, summarize, type JobAction } from '../jobs.js';
import { requireAuth } from '../middleware/requireAuth.js';

export const jobsRouter = Router();

jobsRouter.use(requireAuth);

const ACTIONS: JobAction[] = ['pause', 'resume', 'cancel'];

// ── GET /api/conversations/:id/job ─────────────────────────────────────────
// The conversation's latest background job, or null.
jobsRouter.get('/conversations/:id/job', async (req: Request, res: Response) => {
  if (!/^[a-f0-9]{24}$/i.test(req.params.id)) {
    res.status(404).json({ error: 'not found' });
    return;
  }
  const convo = await conversations().findOne(
    { _id: new ObjectId(req.params.id), userId: req.user!.id },
    { projection: { _id: 1 } },
  );
  if (!convo) {
    res.status(404).json({ error: 'not found' });
    return;
  }
  const job = await latestJob(convo._id);
  res.json({ job: job ? summarize(job) : null });
});

// ── POST /api/jobs/:jobId/:action ──────────────────────────────────────────
// Pause, resume or cancel one of the user's background jobs.
jobsRouter.post('/jobs/:jobId/:action', async (req: Request, res: Response) => {
  const action = req.params.action as JobAction;
  if (!ACTIONS.includes(action)) {
    res.status(404).json({ error: 'unknown action' });
    return;
  }
  const job = await jobs().findOne({ jobId: req.params.jobId, userId: req.user!.id });
  if (!job) {
    res.status(404).json({ error: 'job not found' });
    return;
  }
  const updated = await applyAction(job, action);
  if (!updated) {
    res.status(409).json({ error: `The job is ${job.status}, so it can't be ${action === 'pause' ? 'paused' : action === 'resume' ? 'resumed' : 'cancelled'}.` });
    return;
  }
  res.json({ job: summarize(updated) });
});
