#!/usr/bin/env node
// Find recent X posts where someone voices a need your product meets, and add them to the prospect log.
// Discovery only: triage, drafting and posting are judgment calls made per prospect (see the skill).
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { request, pageTweets } from './twitterapi.mjs';
import { config, own, root } from './config.mjs';
const argv = process.argv.slice(2);
const option = (name, fallback) => { const i = argv.indexOf(name); return i < 0 ? fallback : argv[i + 1]; };
const hours = Number(option('--hours', '48'));
if (!Number.isFinite(hours) || hours <= 0 || hours > 168) throw Error('Invalid --hours');
const only = option('--only', '');
const pages = Number(option('--pages', '1'));
if (!Number.isInteger(pages) || pages < 1 || pages > 5) throw Error('Invalid --pages');

// Searches, skipped accounts and words live in config.json; each search names one need.
const SEARCHES = (config.searches || []).map(s => [s.label, s.query]);
if (!SEARCHES.length) throw Error('Add "searches" to config.json');
const OWN = new Set(own);
const SKIP_ACCOUNTS = new Set((config.skip_accounts || []).map(a => a.replace(/^@/, '').toLowerCase()));
const SKIP_WORDS = (config.skip_words || []).length ? new RegExp(config.skip_words.join('|'), 'i') : null;
const MAX_FOLLOWERS = config.max_followers ?? 150000;

const since = Math.floor(Date.now() / 1000 - hours * 3600);
const found = new Map();
for (const [label, query] of SEARCHES) {
  if (only && !label.includes(only)) continue;
  let tweets = [], cursor = null;
  try {
    for (let page = 0; page < pages; page++) {
      const raw = await request('/twitter/tweet/advanced_search',
        { query: `${query} lang:en -is:retweet since_time:${since}`, queryType: 'Latest', cursor });
      tweets.push(...pageTweets(raw));
      cursor = raw.next_cursor ?? raw.data?.next_cursor;
      if (!(raw.has_next_page ?? raw.data?.has_next_page) || !cursor) break;
    }
  } catch (error) { console.error(JSON.stringify({ search: label, error: error.message })); if (!tweets.length) continue; }
  let kept = 0;
  for (const t of tweets) {
    const author = (t.author?.userName || '').toLowerCase();
    const text = t.note_tweet?.note_tweet_results?.result?.text || t.text || '';
    const followers = t.author?.followers ?? null;
    if (!t.id || OWN.has(author) || SKIP_ACCOUNTS.has(author) || t.retweeted_tweet || text.startsWith('RT @')) continue;
    if ((SKIP_WORDS && SKIP_WORDS.test(text)) || text.replace(/@\w+/g, '').trim().length < 40 || (followers ?? 0) > MAX_FOLLOWERS) continue;
    if (!found.has(String(t.id))) { kept++; found.set(String(t.id), {
      post_id: String(t.id), url: t.url || `https://x.com/${author}/status/${t.id}`, author, followers, text,
      created_at: t.createdAt ? new Date(t.createdAt).toISOString() : null, query: label }); }
  }
  console.log(JSON.stringify({ search: label, returned: tweets.length, kept }));
}
const rows = [...found.values()];
if (argv.includes('--dry-run')) { console.log(JSON.stringify(rows, null, 2)); process.exit(0); }
const result = spawnSync('python3', [join(root, 'scripts/archive.py'), 'prospects', 'add'],
  { input: JSON.stringify(rows), encoding: 'utf8' });
if (result.status !== 0) { console.error(result.stderr); process.exit(1); }
console.log(result.stdout.trim());
