import { Router, Request, Response } from 'express';
import { requireAuth } from '../middleware/requireAuth.js';

export const greetingRouter = Router();

// Greeting is only called from the authenticated ChatBox
greetingRouter.use(requireAuth);

// ── Types ──────────────────────────────────────────────────────────────────

type Period = 'morning' | 'afternoon' | 'evening' | 'night';

interface GreetingPayload {
  greeting: string;
  subtext: string;
}

interface OpenAIResponse {
  choices: Array<{ message: { content: string } }>;
}

// ── Static fallbacks (used when OpenAI is unavailable) ─────────────────────

const FALLBACKS: Record<Period, GreetingPayload> = {
  morning:   { greeting: "Good morning! I'm Booth Radar.", subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you." },
  afternoon: { greeting: "Good afternoon! I'm Booth Radar.", subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you." },
  evening:   { greeting: "Good evening! I'm Booth Radar.", subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you." },
  night:     { greeting: "Good night! I'm Booth Radar.", subtext: "Point me at a trade-show exhibitor directory and I'll collect brands and booth numbers for you." },
};

// ── In-memory cache per period ─────────────────────────────────────────────

interface CacheEntry extends GreetingPayload {
  expiresAt: number;
}

const cache = new Map<Period, CacheEntry>();
const CACHE_TTL_MS = 45 * 60 * 1_000; // 45 minutes

// ── Helpers ────────────────────────────────────────────────────────────────

function getPeriod(hour: number): Period {
  if (hour >= 5  && hour < 12) return 'morning';
  if (hour >= 12 && hour < 17) return 'afternoon';
  if (hour >= 17 && hour < 21) return 'evening';
  return 'night';
}

async function fetchGreeting(period: Period): Promise<GreetingPayload> {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    console.warn('[greeting] OPENAI_API_KEY not set — using static fallback');
    return FALLBACKS[period];
  }

  const response = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: 'gpt-4o-mini',
      temperature: 0.6,
      max_tokens: 120,
      response_format: { type: 'json_object' },
      messages: [
        {
          role: 'system',
          content:
            'You are the welcome screen of Booth Radar, an internal tool that scrapes ' +
            'trade-show exhibitor directories to collect brand names and booth locations. ' +
            'Write in clear, natural, professional English. No wordplay, no slogans, no cleverness.',
        },
        {
          role: 'user',
          content:
            `The user is opening the app in the ${period}. Write a short welcome greeting and one sentence of subtext.\n` +
            'Rules:\n' +
            '- greeting: use a proper time-appropriate salutation (e.g. "Good morning", "Good afternoon", "Good evening", "Good night") followed by a natural phrase that mentions Booth Radar. Maximum 10 words.\n' +
            '- subtext: one clear sentence, 15–20 words, describing what the tool does in plain English — scraping exhibitor directories, collecting brands and booth numbers, exporting as CSV.\n' +
            'Respond only with valid JSON: {"greeting": "...", "subtext": "..."}',
        },
      ],
    }),
  });

  if (!response.ok) {
    throw new Error(`OpenAI responded with ${response.status}`);
  }

  const data = (await response.json()) as OpenAIResponse;
  const raw = data.choices[0]?.message?.content ?? '{}';
  const parsed = JSON.parse(raw) as GreetingPayload;

  if (!parsed.greeting || !parsed.subtext) {
    throw new Error('Unexpected OpenAI response shape');
  }

  return parsed;
}

// ── GET /api/greeting?hour=21 ──────────────────────────────────────────────

greetingRouter.get('/greeting', async (req: Request, res: Response) => {
  const raw  = req.query.hour as string | undefined;
  const hour = parseInt(raw ?? '', 10);

  if (isNaN(hour) || hour < 0 || hour > 23) {
    res.status(400).json({ error: 'hour query param must be an integer 0–23' });
    return;
  }

  const period = getPeriod(hour);

  // Serve cached result if still fresh
  const cached = cache.get(period);
  if (cached && cached.expiresAt > Date.now()) {
    res.json({ greeting: cached.greeting, subtext: cached.subtext });
    return;
  }

  try {
    const payload = await fetchGreeting(period);
    cache.set(period, { ...payload, expiresAt: Date.now() + CACHE_TTL_MS });
    res.json(payload);
  } catch (err) {
    console.error('[greeting] failed to generate greeting:', err);
    res.json(FALLBACKS[period]);
  }
});
