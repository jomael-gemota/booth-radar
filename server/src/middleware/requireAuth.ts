import { Request, Response, NextFunction } from 'express';
import { getUserProfiles } from '../db.js';

/**
 * Express middleware that rejects unauthenticated requests with 401.
 * Apply to any route that requires a signed-in user.
 *
 * Also updates the user's lastSeenAt timestamp and mirrors their Google
 * identity fields (name, email, picture) into the UserProfile document so
 * the admin user-logs page can display accurate info for every user.
 */
export function requireAuth(req: Request, res: Response, next: NextFunction) {
  if (!req.isAuthenticated()) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }

  // Fire-and-forget: update presence + identity fields in the background.
  const { id, name, email, picture } = req.user!;
  getUserProfiles()
    .updateOne(
      { _id: id },
      {
        $set: {
          name,
          email,
          picture,
          lastSeenAt: new Date(),
        },
        $setOnInsert: { updatedAt: new Date() },
      },
      { upsert: true },
    )
    .catch(() => { /* non-critical */ });

  next();
}
