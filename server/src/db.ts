import { MongoClient, Db } from 'mongodb';

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
