import { Router, Request, Response } from 'express';
import { getUserProfiles } from '../db.js';
import { requireAuth } from '../middleware/requireAuth.js';

export const adminRouter = Router();

adminRouter.use(requireAuth);

// ── Admin-only guard ───────────────────────────────────────────────────────

const ADMIN_EMAIL = 'jomael@outdoorequipped.com';

function requireAdmin(req: Request, res: Response, next: () => void) {
  if (req.user!.email !== ADMIN_EMAIL) {
    res.status(403).json({ error: 'Forbidden' });
    return;
  }
  next();
}

// ── GET /api/admin/users ───────────────────────────────────────────────────
// Returns all user profiles with online/offline presence status.
// Only accessible by the admin account.
adminRouter.get('/admin/users', requireAdmin, async (_req: Request, res: Response) => {
  const profiles = await getUserProfiles()
    .find({})
    .sort({ lastSeenAt: -1 })
    .toArray();

  const ONLINE_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes
  const now = Date.now();

  const users = profiles.map((p) => ({
    id:         p._id,
    name:       p.name       ?? '(unknown)',
    email:      p.email      ?? '(unknown)',
    picture:    p.picture    ?? null,
    nickname:   p.nickname   ?? null,
    jobTitle:   p.jobTitle   ?? null,
    department: p.department ?? null,
    lastSeenAt: p.lastSeenAt ?? null,
    online:     p.lastSeenAt != null && (now - new Date(p.lastSeenAt).getTime()) < ONLINE_THRESHOLD_MS,
  }));

  res.json({ users });
});
