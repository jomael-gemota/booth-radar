import { Request, Response, NextFunction } from 'express';

/**
 * Express middleware that rejects unauthenticated requests with 401.
 * Apply to any route that requires a signed-in user.
 */
export function requireAuth(req: Request, res: Response, next: NextFunction) {
  if (req.isAuthenticated()) return next();
  res.status(401).json({ error: 'Unauthorized' });
}
