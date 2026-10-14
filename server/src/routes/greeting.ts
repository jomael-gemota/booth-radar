import { Router, Request, Response } from 'express';
import { requireAuth } from '../middleware/requireAuth.js';

export const greetingRouter = Router();

// Greeting is only called from the authenticated ChatBox
greetingRouter.use(requireAuth);

// ── Types ──────────────────────────────────────────────────────────────────

type Period = 'morning' | 'afternoon' | 'evening' | 'night';

interface GreetingPayload {
  greeting: string;
  subtext:  string;
}

interface OpenAIResponse {
  choices: Array<{ message: { content: string } }>;
}

// ── Static fallbacks (used when OpenAI is unavailable) ─────────────────────

function buildFallback(period: Period, name: string): GreetingPayload {
  const prefix: Record<Period, string> = {
    morning:   'Good morning',
    afternoon: 'Good afternoon',
    evening:   'Good evening',
    night:     'Good night',
  };
  const greeting = name
    ? `${prefix[period]}, ${name}! I'm Booth Radar.`
    : `${prefix[period]}! I'm Booth Radar.`;
  return {
    greeting,
    subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you.",
  };
}

// ── In-memory cache keyed by "period:name" ─────────────────────────────────

interface CacheEntry extends GreetingPayload {
  expiresAt: number;
}

// Key: `${period}:${name}` — personalised greetings are cached per user name
const cache = new Map<string, CacheEntry>();
const CACHE_TTL_MS = 45 * 60 * 1_000; // 45 minutes

// ── Helpers ────────────────────────────────────────────────────────────────

function getPeriod(hour: number): Period {
  if (hour >= 5  && hour < 12) return 'morning';
  if (hour >= 12 && hour < 17) return 'afternoon';
  if (hour >= 17 && hour < 21) return 'evening';
  return 'night';
}

async function fetchGreeting(period: Period, name: string): Promise<GreetingPayload> {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    console.warn('[greeting] OPENAI_API_KEY not set — using static fallback');
    return buildFallback(period, name);
  }

  const nameInstruction = name
    ? `The user's name is "${name}". Address them by first name in the greeting naturally.`
    : '';

  const response = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization:  `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model:           'gpt-4o-mini',
      temperature:     0.6,
      max_tokens:      120,
      response_format: { type: 'json_object' },
      messages: [
        {
          role:    'system',
          content:
            'You are the welcome screen of Booth Radar, an internal tool that scrapes ' +
            'trade-show exhibitor directories to collect brand names and booth locations. ' +
            'Write in clear, natural, professional English. No wordplay, no slogans, no cleverness.',
        },
        {
          role:    'user',
          content:
            `The user is opening the app in the ${period}. ${nameInstruction} ` +
            'Write a short welcome greeting and one sentence of subtext.\n' +
            'Rules:\n' +
            `- greeting: use a proper time-appropriate salutation${name ? ` and address the user as "${name}"` : ''} ` +
            '(e.g. "Good morning, Alex! Welcome back to Booth Radar."). Maximum 12 words.\n' +
            '- subtext: one clear sentence, 15–20 words, describing what the tool does in plain English — scraping exhibitor directories, collecting brands and booth numbers, exporting as CSV.\n' +
            'Respond only with valid JSON: {"greeting": "...", "subtext": "..."}',
        },
      ],
    }),
  });

  if (!response.ok) {
    throw new Error(`OpenAI responded with ${response.status}`);
  }

  const data   = (await response.json()) as OpenAIResponse;
  const raw    = data.choices[0]?.message?.content ?? '{}';
  const parsed = JSON.parse(raw) as GreetingPayload;

  if (!parsed.greeting || !parsed.subtext) {
    throw new Error('Unexpected OpenAI response shape');
  }

  return parsed;
}

// ── GET /api/greeting?hour=21&name=Alex ────────────────────────────────────

greetingRouter.get('/greeting', async (req: Request, res: Response) => {
  const raw  = req.query.hour as string | undefined;
  const hour = parseInt(raw ?? '', 10);

  if (isNaN(hour) || hour < 0 || hour > 23) {
    res.status(400).json({ error: 'hour query param must be an integer 0–23' });
    return;
  }

  // Sanitise name: trim, max 48 chars, strip anything that could mess with the prompt
  const rawName = (req.query.name as string | undefined) ?? '';
  const name    = rawName.trim().slice(0, 48).replace(/[^\p{L}\p{N} '\-\.]/gu, '');

  const period   = getPeriod(hour);
  const cacheKey = `${period}:${name.toLowerCase()}`;

  // Serve cached result if still fresh
  const cached = cache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now()) {
    res.json({ greeting: cached.greeting, subtext: cached.subtext });
    return;
  }

  try {
    const payload = await fetchGreeting(period, name);
    cache.set(cacheKey, { ...payload, expiresAt: Date.now() + CACHE_TTL_MS });
    res.json(payload);
  } catch (err) {
    console.error('[greeting] failed to generate greeting:', err);
    res.json(buildFallback(period, name));
  }
});
