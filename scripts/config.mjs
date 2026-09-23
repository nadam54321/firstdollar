// Shared config for the Node scripts: config.json at the repo root, or FIRSTDOLLAR_CONFIG.
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

export const root = fileURLToPath(new URL('../', import.meta.url));
const path = process.env.FIRSTDOLLAR_CONFIG || join(root, 'config.json');
if (!existsSync(path)) throw Error('Copy config.example.json to config.json and fill it in');
export const config = JSON.parse(readFileSync(path, 'utf8'));
export const own = (config.accounts || []).map(a => a.replace(/^@/, '').toLowerCase());
