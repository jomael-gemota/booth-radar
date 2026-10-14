import { useEffect, useState } from 'react';

// ── Types ─────────────────────────────────────────────────────────────────

export interface AuthUser {
  id: string;
  name: string;
  email: string;
  picture: string;
}

export type AuthState =
  | { status: 'loading' }
  | { status: 'authenticated'; user: AuthUser }
  | { status: 'unauthenticated' };

// ── Hook ──────────────────────────────────────────────────────────────────

export function useAuth(): AuthState {
  const [state, setState] = useState<AuthState>({ status: 'loading' });

  useEffect(() => {
    fetch('/api/auth/me', { credentials: 'include' })
      .then((r) => {
        if (r.status === 401) {
          setState({ status: 'unauthenticated' });
          return null;
        }
        return r.json() as Promise<{ user: AuthUser }>;
      })
      .then((data) => {
        if (data) setState({ status: 'authenticated', user: data.user });
      })
      .catch(() => {
        setState({ status: 'unauthenticated' });
      });
  }, []);

  return state;
}

// ── Logout helper ─────────────────────────────────────────────────────────

export async function logout(): Promise<void> {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
  window.location.reload();
}
