#!/usr/bin/env node
// Post the replies a person has approved, one at a time, inside the daily limit and spacing.
//   node scripts/post.mjs --login        sign in to X once in the dedicated browser profile
//   node scripts/post.mjs --dry-run      fill each reply box and stop before Reply
//   node scripts/post.mjs                post approved replies
//   node scripts/post.mjs --cdp http://127.0.0.1:9222   use an already-running, signed-in Chrome
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { root } from './config.mjs';
import { openBrowser } from './browser.mjs';
import { postReply, Stop } from './x-reply.mjs';

const argv = process.argv.slice(2);
const option = (name, fallback) => { const i = argv.indexOf(name); return i < 0 ? fallback : argv[i + 1]; };
const dryRun = argv.includes('--dry-run');
const only = option('--only', null);

function archive(...args) {
  const r = spawnSync('python3', [join(root, 'scripts/archive.py'), ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  if (r.status !== 0) throw Error(r.stderr.trim() || 'archive.py ' + args[0] + ' failed');
  return r.stdout.trim();
}

const { context, close } = await openBrowser(option);
try {
  if (argv.includes('--login')) {
    const page = await context.newPage();
    await page.goto('https://x.com/login');
    console.log('Sign in to X in the opened window, then close that tab.');
    await page.waitForEvent('close', { timeout: 0 });
    process.exit(0);
  }
  let replies = JSON.parse(archive('queue', '--status', 'approved', '--json'));
  if (only) replies = replies.filter(r => String(r.id) === only);
  if (!replies.length) { console.log('No approved replies.'); process.exit(0); }
  const page = await context.newPage();
  for (const r of replies) {
    const limit = JSON.parse(archive('limits'));
    if (!dryRun && limit.remaining_today <= 0) { console.log(`Daily limit of ${limit.daily_limit} reached; the rest wait for tomorrow.`); break; }
    if (!dryRun && limit.wait_seconds > 0) {
      console.log(`Waiting ${limit.wait_seconds}s between replies…`);
      await new Promise(done => setTimeout(done, limit.wait_seconds * 1000));
    }
    const reply = { url: r.url, text: r.text, untick: JSON.parse(r.untick) };
    try {
      const result = await postReply(page, reply, { dryRun });
      if (dryRun) { console.log(`#${r.id} ready in the reply box for @${r.author} (dry run, not posted)`); break; }
      const id = result.replyUrl?.match(/status\/(\d+)/)?.[1];
      archive('posted', String(r.id), ...(id ? ['--reply', id] : []));
      console.log(`#${r.id} posted to @${r.author}${result.replyUrl ? ': ' + result.replyUrl : ''}`);
    } catch (error) {
      const note = error instanceof Stop ? error.message : 'Unexpected browser error: ' + error.message.split('\n')[0];
      if (!dryRun) archive('failed', String(r.id), '--note', note);
      console.error(`#${r.id} stopped: ${note}`);
      break;  // one doubt stops the whole run
    }
  }
} finally {
  await close();
}
