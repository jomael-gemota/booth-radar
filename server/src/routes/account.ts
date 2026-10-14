import { Router, Request, Response } from 'express';
import { getUserProfiles } from '../db.js';
import { requireAuth } from '../middleware/requireAuth.js';

export const accountRouter = Router();

accountRouter.use(requireAuth);

// ── GET /api/me ────────────────────────────────────────────────────────────
// Returns the current user's Google identity merged with their stored profile.
accountRouter.get('/me', async (req: Request, res: Response) => {
  const { id, name, email, picture } = req.user!;
  const stored = await getUserProfiles().findOne({ _id: id });

  res.json({
    id,
    name,
    email,
    picture,
    nickname:   stored?.nickname   ?? null,
    jobTitle:   stored?.jobTitle   ?? null,
    department: stored?.department ?? null,
  });
});

// ── PATCH /api/me ──────────────────────────────────────────────────────────
// Updates the mutable profile fields.
accountRouter.patch('/me', async (req: Request, res: Response) => {
  const { id } = req.user!;
  const { nickname, jobTitle, department } = req.body as {
    nickname?:   string;
    jobTitle?:   string;
    department?: string;
  };

  // Build update — only include provided fields; empty string clears the field.
  const update: Record<string, string | null> = {};
  if (nickname   !== undefined) update.nickname   = nickname?.trim()   || '';
  if (jobTitle   !== undefined) update.jobTitle   = jobTitle?.trim()   || '';
  if (department !== undefined) update.department = department?.trim() || '';

  if (Object.keys(update).length === 0) {
    res.status(400).json({ error: 'No fields to update' });
    return;
  }

  await getUserProfiles().updateOne(
    { _id: id },
    { $set: { ...update, updatedAt: new Date() } },
    { upsert: true },
  );

  res.json({ ok: true });
});
