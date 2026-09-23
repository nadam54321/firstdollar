#!/usr/bin/env node
// Check the setup and say what to fix. Reads nothing from X and posts nothing.
import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const results = [];
const check = (ok, what, fix) => results.push({ ok, what, fix });

check(Number(process.versions.node.split('.')[0]) >= 22, `Node ${process.versions.node}`, 'Install Node 22 or newer');
const py = spawnSync('python3', ['-c', 'import sys, zoneinfo, sqlite3; print(sys.version.split()[0])'], { encoding: 'utf8' });
check(py.status === 0, `Python ${py.stdout.trim() || 'missing'}`, 'Install Python 3.9 or newer');

const configPath = process.env.FIRSTDOLLAR_CONFIG || join(root, 'config.json');
let config = null;
try { config = JSON.parse(readFileSync(configPath, 'utf8')); } catch {}
check(!!config, 'config.json', 'cp config.example.json config.json and fill it in');
check(!!config?.accounts?.length && !config.accounts.includes('your_handle'), 'config: your X accounts', 'Set "accounts" to your handles');
check(!!config?.searches?.length && !JSON.stringify(config.searches).includes('your category words'), 'config: searches for your needs', 'Replace the example searches with your own');
check(!!config?.product_url, 'config: product_url', 'Set "product_url" so the agent can read your site during onboarding');

const brand = join(root, 'brand.md');
const template = readFileSync(join(root, 'brand.template.md'), 'utf8');
check(existsSync(brand) && readFileSync(brand, 'utf8').trim() !== template.trim(), 'brand.md filled in', 'Ask your agent to run onboarding (AGENTS.md) or fill brand.md yourself');
check(!!process.env.TWITTERAPI_IO_API_KEY, 'TWITTERAPI_IO_API_KEY', 'export TWITTERAPI_IO_API_KEY=... (https://twitterapi.io)');

let chromeOk = false;
try {
  const { chromium } = await import('playwright-core');
  const b = await chromium.launch({ channel: config?.browser?.channel ?? 'chrome', headless: true });
  await b.close(); chromeOk = true;
} catch {}
check(chromeOk, `browser (${config?.browser?.channel ?? 'chrome'})`, 'npm install, and install Google Chrome or set "browser.channel"');
check(existsSync(join(root, 'data/browser-profile')), 'X sign-in profile', 'node scripts/post.mjs --login');

for (const r of results) console.log(`${r.ok ? '✓' : '✗'} ${r.what}${r.ok ? '' : `  →  ${r.fix}`}`);
process.exitCode = results.every(r => r.ok) ? 0 : 1;
