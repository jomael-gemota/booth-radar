import passport from 'passport';
import { Strategy as GoogleStrategy } from 'passport-google-oauth20';

// ── User shape (stored in the session) ────────────────────────────────────

export interface AppUser {
  id: string;
  name: string;
  email: string;
  picture: string;
}

// Merge into Express's User type
declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    // eslint-disable-next-line @typescript-eslint/no-empty-object-type
    interface User extends AppUser {}
  }
}

// ── Configure Passport ────────────────────────────────────────────────────

let googleConfigured = false;

/** Returns true if Google OAuth was configured at startup. */
export function isGoogleConfigured(): boolean {
  return googleConfigured;
}

export function configurePassport() {
  const GOOGLE_CLIENT_ID     = process.env.GOOGLE_CLIENT_ID;
  const GOOGLE_CLIENT_SECRET = process.env.GOOGLE_CLIENT_SECRET;
  const BASE_URL             = process.env.BASE_URL ?? 'http://localhost:3001';

  if (!GOOGLE_CLIENT_ID || !GOOGLE_CLIENT_SECRET) {
    console.warn('[auth] GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set — Google OAuth disabled');
    return;
  }

  // Optional domain allowlist (comma-separated, e.g. "yourco.com,partner.com")
  const allowedDomains = process.env.ALLOWED_EMAIL_DOMAINS
    ? process.env.ALLOWED_EMAIL_DOMAINS.split(',').map((d) => d.trim().toLowerCase()).filter(Boolean)
    : [];

  passport.use(
    new GoogleStrategy(
      {
        clientID:     GOOGLE_CLIENT_ID,
        clientSecret: GOOGLE_CLIENT_SECRET,
        callbackURL:  `${BASE_URL}/api/auth/google/callback`,
      },
      (_accessToken, _refreshToken, profile, done) => {
        const email   = profile.emails?.[0]?.value ?? '';
        const picture = profile.photos?.[0]?.value ?? '';

        // Domain restriction (if configured)
        if (allowedDomains.length > 0) {
          const domain = email.split('@')[1]?.toLowerCase() ?? '';
          if (!allowedDomains.includes(domain)) {
            return done(null, false);
          }
        }

        const user: AppUser = {
          id:      profile.id,
          name:    profile.displayName,
          email,
          picture,
        };

        return done(null, user);
      },
    ),
  );

  // Store the entire user object in the session — no DB lookup needed on each request
  passport.serializeUser<AppUser>((user, done) => done(null, user));
  passport.deserializeUser<AppUser>((user, done) => done(null, user));

  googleConfigured = true;
  console.log('[auth] Google OAuth configured');
}
