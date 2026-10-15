import { MongoClient, Db, Collection } from 'mongodb';

const MONGODB_URI = process.env.MONGODB_URI ?? 'mongodb://localhost:27017';
const MONGODB_DB  = process.env.MONGODB_DB  ?? 'booth-radar';

let db: Db;

export async function connectDb(): Promise<Db> {
  if (db) return db;
  const client = new MongoClient(MONGODB_URI);
  await client.connect();
  db = client.db(MONGODB_DB);
  console.log(`[db] connected to ${MONGODB_DB}`);
  return db;
}

export function getDb(): Db {
  if (!db) throw new Error('DB not initialised — call connectDb() first');
  return db;
}

// ── User profiles ──────────────────────────────────────────────────────────

export interface UserProfile {
  _id: string;           // Google user ID
  nickname?: string;
  jobTitle?: string;
  department?: string;
  updatedAt: Date;
  // Identity fields mirrored from Google (populated on first/each login)
  name?: string;
  email?: string;
  picture?: string;
  // Presence tracking
  lastSeenAt?: Date;
}

export function getUserProfiles(): Collection<UserProfile> {
  return getDb().collection<UserProfile>('userProfiles');
}
