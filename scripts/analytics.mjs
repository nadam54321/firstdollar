#!/usr/bin/env node
// Download your X analytics exports in the signed-in browser and import them.
//   node scripts/analytics.mjs            try the Export buttons automatically
//   node scripts/analytics.mjs --manual   you click Export (posts and overview); close the tab when done
import { mkdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { own, root } from './config.mjs';
import { openBrowser } from './browser.mjs';
import { downloadExports, collectManually, Stop } from './x-analytics.mjs';

const argv = process.argv.slice(2);
const option = (name, fallback) => { const i = argv.indexOf(name); return i < 0 ? fallback : argv[i + 1]; };
const account = option('--account', own[0]);
const dir = join(root, 'data/exports', new Date().toISOString().slice(0, 10));
mkdirSync(dir, { recursive: true });

const { context, close } = await openBrowser(option);
let files = [];
try {
  const page = await context.newPage();
  if (argv.includes('--manual')) {
    console.log('In the opened window, export posts and the account overview, then close the tab.');
    files = await collectManually(page, dir);
  } else {
    try { files = await downloadExports(page, { dir }); }
    catch (error) {
      if (!(error instanceof Stop)) throw error;
      console.error(error.message + '. Run again with --manual to click Export yourself.');
      process.exitCode = 1;
    }
  }
} finally { await close(); }
for (const file of files.filter(f => f.endsWith('.csv'))) {
  const r = spawnSync('python3', [join(root, 'scripts/archive.py'), 'import-analytics', file, ...(account ? ['--account', account] : [])], { encoding: 'utf8' });
  console.log(r.status === 0 ? `imported ${file}: ${r.stdout.trim()}` : `could not import ${file}: ${r.stderr.trim()}`);
}
