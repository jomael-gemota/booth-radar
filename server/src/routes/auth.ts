import { Router, Request, Response, NextFunction } from 'express';
import passport from 'passport';
import { isGoogleConfigured } from '../auth.js';

export const authRouter = Router();

// ── GET /api/auth/me ───────────────────────────────────────────────────────
// Returns the current user or 401 if not authenticated.
authRouter.get('/auth/me', (req: Request, res: Response) => {
  if (!req.isAuthenticated()) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }
  res.json({ user: req.user });
});

// ── GET /api/auth/google ───────────────────────────────────────────────────
// Redirects the browser to Google's OAuth consent screen.
authRouter.get(
  '/auth/google',
  (req: Request, res: Response, next: NextFunction) => {
    if (!isGoogleConfigured()) {
      res.status(503).json({
        error: 'Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.',
      });
      return;
    }
    next();
  },
  passport.authenticate('google', { scope: ['profile', 'email'] }),
);

// ── GET /api/auth/google/callback ──────────────────────────────────────────
// Google redirects back here after the user grants/denies access.
authRouter.get(
  '/auth/google/callback',
  passport.authenticate('google', { failureRedirect: '/?auth=failed' }),
  (_req: Request, res: Response) => {
    res.redirect('/');
  },
);

// ── POST /api/auth/logout ──────────────────────────────────────────────────
authRouter.post('/auth/logout', (req: Request, res: Response) => {
  req.logout(() => {
    req.session.destroy(() => {
      res.clearCookie('connect.sid');
      res.json({ ok: true });
    });
  });
});
