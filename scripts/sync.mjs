#!/usr/bin/env node
// Read-only TwitterAPI.io sync into the local archive.
import { existsSync, mkdirSync, writeFileSync, renameSync, readFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { pagination, refreshIds } from './sync-state.mjs';
import { request, pageTweets } from './twitterapi.mjs';
import { own } from './config.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const argv = process.argv.slice(2);
function option(name, fallback) { const i=argv.indexOf(name); return i<0?fallback:argv[i+1]; }
const account=option('--account',own[0]||'').replace(/^@/,'').toLowerCase();
if (!/^\w{1,15}$/.test(account)) throw Error('Set "accounts" in config.json or pass --account');
const maxPages=Number(option('--max-pages','50'));
if (!Number.isInteger(maxPages) || maxPages<1 || maxPages>500) throw Error('Invalid page limit');
const mode=option('--mode','search');
if (!['search','timeline'].includes(mode)) throw Error('Invalid mode');
const full=argv.includes('--full');
const refresh=argv.includes('--refresh');
const days=Number(option('--days','8'));
const db=resolve(option('--db',join(root,'data/leads.sqlite3')));
const statePath=join(root,`data/sync-${account}-${mode}.json`);
mkdirSync(join(root,'data'),{recursive:true});
function archive(args, input) {
  const result=spawnSync('python3',[join(root,'scripts/archive.py'),'--db',db,...args],
    {input: input===undefined?undefined:JSON.stringify(input),encoding:'utf8',maxBuffer:32*1024*1024});
  if(result.status!==0) throw Error('Archive command failed: '+args[0]);
  return result.stdout.trim();
}
function flatten(tweets){
  const rows=new Map();
  function add(t){
    if(!t?.id||rows.has(t.id)) return;
    const author=t.author||{};
    const metricNames={likes:'likeCount',retweets:'retweetCount',replies:'replyCount',quotes:'quoteCount',views:'viewCount',bookmarks:'bookmarkCount'};
    rows.set(t.id, {id:String(t.id),url:t.url||`https://x.com/${author.userName||'i'}/status/${t.id}`,
      text:t.note_tweet?.note_tweet_results?.result?.text || t.text || '',created_at:t.createdAt,
      author:{username:author.userName,id:author.id},
      metrics:Object.fromEntries(Object.entries(metricNames).map(([k,v])=>[k,t[v]??null])),
      is_reply:t.isReply,in_reply_to_id:t.inReplyToId||null,conversation_id:t.conversationId,
      quoted_id:t.quoted_tweet?.id||null,reposted_id:t.retweeted_tweet?.id||null,
      source_tweet:t});
    add(t.quoted_tweet);add(t.retweeted_tweet);
  }
  tweets.forEach(add);return [...rows.values()];
}
function save(raw,tweets,query,cursor,command){
  const body=raw.data||raw;
  const normalized={success:true,source:'twitterapi.io/raw',command,query,
    data:flatten(tweets),raw_response:raw,
    has_next_page:raw.has_next_page??body.has_next_page,
    next_cursor:raw.next_cursor??body.next_cursor??null};
  archive(['import',...(cursor?['--cursor',cursor]:[])],normalized);
}
function finish(summary){
  if(!argv.includes('--no-backup')) summary.backup=archive(['backup']);
  console.log(JSON.stringify(summary));
}
let existing=JSON.parse(archive(['ids','--author',account]));
if(refresh){
  // Metrics only: re-read recent own posts by ID so reports can compare them at the same age.
  // --ids fetches named posts that search never returned, such as rows found in an X analytics export.
  const named=option('--ids','');
  if(named&&!/^\d+(,\d+)*$/.test(named)) throw Error('Invalid --ids list');
  const ids=named?named.split(','):refreshIds(existing,days);
  let found=0;
  for(let i=0;i<ids.length;i+=20){
    const batch=ids.slice(i,i+20);
    const raw=await request('/twitter/tweets',{tweet_ids:batch.join(',')});
    const tweets=pageTweets(raw);save(raw,tweets,batch.join(','),null,'refresh');
    found+=tweets.filter(t=>batch.includes(String(t.id))).length;
  }
  finish({account,refresh_days:days,requested:ids.length,refreshed:found});
  process.exit(0);
}
let query=`from:${account}`;
if(!full&&existing.length){
  const since=Math.floor(Date.parse(existing[0].created_at)/1000)-2*86400;
  query+=` since_time:${since}`;
}
let cursor=null;
if(argv.includes('--resume')&&existsSync(statePath)){
  const state=JSON.parse(readFileSync(statePath,'utf8'));
  if(state.database!==db) throw Error('Resume database mismatch');
  if(state.next_cursor){cursor=state.next_cursor;query=state.query;}
}
const seen=new Set();let exhausted=false;let count=0;let pages=0;
for(;pages<maxPages;pages++){
  const raw=await request(mode==='search'?'/twitter/tweet/advanced_search':'/twitter/user/last_tweets',
    mode==='search'?{query,queryType:'Latest',cursor}:{userName:account,includeReplies:true,cursor});
  const tweets=pageTweets(raw);
  const {exhausted:done,next}=pagination(raw,cursor,seen);
  save(raw,tweets,query,cursor,mode);
  count+=tweets.length;
  exhausted=done;
  const state={account,mode,database:db,query,next_cursor:exhausted?null:next,exhausted,
    pages_this_run:pages+1,results_this_run:count,observed_at:new Date().toISOString(),
    coverage:'API-visible results only; not a complete account export'};
  writeFileSync(statePath+'.tmp',JSON.stringify(state,null,2));renameSync(statePath+'.tmp',statePath);
  console.log(JSON.stringify({account,page:pages+1,count:tweets.length,exhausted}));
  if(exhausted)break;
  seen.add(next);cursor=next;
}
// Fetch direct parent context in batches. Missing/deleted posts are recorded, never silently filled.
let missing=JSON.parse(archive(['missing-parents','--author',account,'--limit','10000']));
let parents=0;
for(let i=0;i<missing.length;i+=20){
  const ids=missing.slice(i,i+20);
  const raw=await request('/twitter/tweets',{tweet_ids:ids.join(',')});
  const tweets=pageTweets(raw);save(raw,tweets,ids.join(','),null,'parent-context');
  const found=new Set(tweets.map(t=>String(t.id)));parents+=found.size;
  for(const id of ids)if(!found.has(id))archive(['context-failed',id,'Not returned by API; may be deleted, private or unavailable']);
}
finish({account,results:count,parent_posts:parents,exhausted});
if(!exhausted){console.error('Page limit reached; resume with --resume.');process.exitCode=2;}
