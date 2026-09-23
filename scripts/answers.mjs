#!/usr/bin/env node
// Who replied to you in the last few days, matched with the people you answered. Read-only.
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { request, pageTweets } from './twitterapi.mjs';
import { own, root } from './config.mjs';

const argv = process.argv.slice(2);
const hours = Number(argv[argv.indexOf('--hours') + 1] || 72);
const since = Math.floor(Date.now() / 1000 - (argv.includes('--hours') ? hours : 72) * 3600);
const rows = new Map();
for (const handle of own) {
  const raw = await request('/twitter/tweet/advanced_search', { query: `to:${handle} -from:${handle} since_time:${since}`, queryType: 'Latest' });
  for (const t of pageTweets(raw)) {
    const author = (t.author?.userName || '').toLowerCase();
    if (!own.includes(author)) rows.set(String(t.id), { id: String(t.id), url: t.url, author, text: t.text || '', created_at: t.createdAt, to: handle });
  }
}
const r = spawnSync('python3', [join(root, 'scripts/archive.py'), 'answers'], { input: JSON.stringify([...rows.values()]), encoding: 'utf8' });
if (r.status !== 0) { console.error(r.stderr); process.exit(1); }
const seen = JSON.parse(r.stdout);
const leads = seen.filter(s => s.lead), others = seen.filter(s => !s.lead);
console.log(`Replies to you in the last ${argv.includes('--hours') ? hours : 72} hours: ${seen.length} (${leads.length} from people you pitched)\n`);
for (const s of [...leads, ...others]) {
  console.log(`${s.lead ? `[lead: ${s.lead.stage}]` : '[other]'} @${s.author} ${s.url}\n    ${s.text.replace(/\s+/g, ' ').slice(0, 280)}\n`);
}
