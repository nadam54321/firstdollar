#!/usr/bin/env python3
"""Local archive, prospect log and reply queue for finding customers on X.

Standard library only. This script never posts; scripts/post.mjs posts approved replies."""
import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get('FIRSTDOLLAR_DB', ROOT / 'data' / 'leads.sqlite3'))
CONFIG_PATH = Path(os.environ.get('FIRSTDOLLAR_CONFIG', ROOT / 'config.json'))


def load_config(path=CONFIG_PATH):
    config = {'accounts': [], 'timezone': 'UTC', 'themes': [], 'competitors': [], 'daily_reply_limit': 10,
              'min_minutes_between_posts': 3}
    if Path(path).exists():
        config.update(json.loads(Path(path).read_text()))
    return config


CONFIG = load_config()
OWN = tuple(a.lstrip('@').lower() for a in CONFIG['accounts'])
LOCAL = ZoneInfo(CONFIG['timezone'])
# Optional reviewed themes, one per post; an empty list allows any theme.
THEMES = tuple(CONFIG['themes'])
# A post you deleted stays archived as history but leaves the performance report.
STATUSES = ('deleted',)
# Hours after posting: an observation inside the window, closest to its target, stands for that age.
WINDOWS = {'day1': (12, 36, 24), 'day7': (144, 192, 168)}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def check(db):
    integrity = [r[0] for r in db.execute('PRAGMA integrity_check')]
    if integrity != ['ok'] or db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Archive integrity or foreign-key check failed')


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS posts (
        id TEXT PRIMARY KEY, url TEXT, author TEXT, text TEXT NOT NULL,
        created_at TEXT, is_reply INTEGER, parent_id TEXT, conversation_id TEXT,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, raw_json TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS posts_author_date ON posts(author,created_at);
      CREATE INDEX IF NOT EXISTS posts_parent ON posts(parent_id);
      CREATE TABLE IF NOT EXISTS relationships (
        post_id TEXT REFERENCES posts(id), kind TEXT, target_id TEXT,
        PRIMARY KEY(post_id,kind,target_id)
      );
      CREATE TABLE IF NOT EXISTS post_versions (
        post_id TEXT REFERENCES posts(id), text_hash TEXT, text TEXT,
        observed_at TEXT, PRIMARY KEY(post_id,text_hash)
      );
      CREATE TABLE IF NOT EXISTS observations (
        post_id TEXT REFERENCES posts(id), observed_at TEXT, metrics_json TEXT,
        PRIMARY KEY(post_id,observed_at)
      );
      CREATE TABLE IF NOT EXISTS imports (
        id INTEGER PRIMARY KEY, source TEXT, command TEXT, query TEXT,
        requested_cursor TEXT, next_cursor TEXT, has_next_page INTEGER,
        count INTEGER, observed_at TEXT, payload_hash TEXT UNIQUE,
        raw_json TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS annotations (
        post_id TEXT REFERENCES posts(id), category TEXT, value TEXT,
        provenance TEXT NOT NULL, updated_at TEXT,
        PRIMARY KEY(post_id,category,value)
      );
      CREATE TABLE IF NOT EXISTS replies (
        id INTEGER PRIMARY KEY, prospect_id TEXT NOT NULL, text TEXT NOT NULL,
        untick TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','posted','rejected','failed')),
        reply_id TEXT, note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, posted_at TEXT
      );
      CREATE TABLE IF NOT EXISTS context_attempts (
        post_id TEXT PRIMARY KEY, attempted_at TEXT, error TEXT
      );
      CREATE TABLE IF NOT EXISTS analytics_exports (
        post_id TEXT, exported_at TEXT, source_file TEXT, metrics_json TEXT NOT NULL,
        PRIMARY KEY(post_id,exported_at)
      );
      CREATE TABLE IF NOT EXISTS prospects (
        post_id TEXT PRIMARY KEY, url TEXT, author TEXT, followers INTEGER, text TEXT NOT NULL,
        created_at TEXT, query TEXT, found_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','skipped','replied')),
        reason TEXT, reply_id TEXT, updated_at TEXT
      );
      CREATE TABLE IF NOT EXISTS daily_signals (
        day TEXT, key TEXT, value REAL NOT NULL, source TEXT NOT NULL, note TEXT, updated_at TEXT,
        PRIMARY KEY(day,key,source)
      );
    ''')
    # Sales stage after our reply: they answered, have the price and link, or ordered (shop-confirmed).
    if 'stage' not in [r[1] for r in db.execute('PRAGMA table_info(prospects)')]:
        db.execute('ALTER TABLE prospects ADD COLUMN stage TEXT')
    return db


STAGES = ('answered', 'offered', 'ordered')


def timestamp(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).isoformat()


def unwrap(value):
    if 'content' in value:
        return json.loads(next(c['text'] for c in value['content'] if c['type'] == 'text'))
    return value


def ingest(db, payload, requested_cursor=None, observed_at=None):
    payload = unwrap(payload)
    if payload.get('success') is not True:
        raise ValueError('Connector did not report success; page not imported')
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if db.execute('SELECT 1 FROM imports WHERE payload_hash=?', (digest,)).fetchone():
        return {'imported': 0, 'duplicate_payload': True}
    rows = payload.get('data')
    if isinstance(rows, dict):
        rows = [rows] if rows.get('id') else []
    if not isinstance(rows, list):
        raise ValueError('Expected normalized connector data list or tweet object')
    seen = observed_at or now()
    # Validate entire batch before any mutation.
    for row in rows:
        if not isinstance(row.get('id'), str) or not row['id'].isdigit() or not isinstance(row.get('text'), str):
            raise ValueError('Missing tweet ID/text; refusing partial batch')
        timestamp(row.get('created_at'))
    with db:
        for row in rows:
            author = (row.get('author') or {}).get('username', '').lower() or None
            text = row['text']
            db.execute('''INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET url=excluded.url,author=excluded.author,
              text=excluded.text,created_at=excluded.created_at,is_reply=excluded.is_reply,
              parent_id=excluded.parent_id,conversation_id=excluded.conversation_id,
              last_seen=excluded.last_seen,raw_json=excluded.raw_json''',
              (row['id'], row.get('url'), author, text, timestamp(row.get('created_at')),
               int(row['is_reply']) if row.get('is_reply') is not None else None,
               row.get('in_reply_to_id'), row.get('conversation_id'), seen, seen,
               json.dumps(row, ensure_ascii=False)))
            db.execute('INSERT OR IGNORE INTO post_versions VALUES(?,?,?,?)',
                       (row['id'], hashlib.sha256(text.encode()).hexdigest(), text, seen))
            # Missing metrics stay unknown, not zero.
            db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?)',
                       (row['id'], seen, json.dumps(row.get('metrics') or {})))
            db.execute('DELETE FROM context_attempts WHERE post_id=?', (row['id'],))
            for kind, key in [('quote','quoted_id'),('repost','reposted_id')]:
                if row.get(key):
                    db.execute('INSERT OR IGNORE INTO relationships VALUES(?,?,?)', (row['id'],kind,row[key]))
        db.execute('''INSERT INTO imports(source,command,query,requested_cursor,next_cursor,
          has_next_page,count,observed_at,payload_hash,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
          (payload.get('source'), payload.get('command'), payload.get('query') or payload.get('username'),
           requested_cursor, payload.get('next_cursor'),
           int(payload['has_next_page']) if 'has_next_page' in payload else None,
           len(rows), seen, digest, raw))
    return {'imported': len(rows), 'next_cursor': payload.get('next_cursor'),
            'has_next_page': payload.get('has_next_page')}


def tag(db, post_id, category, value, provenance):
    if category == 'theme' and THEMES and value not in THEMES:
        raise ValueError('Unknown theme; choose one of: ' + ', '.join(THEMES))
    if category == 'status' and value not in STATUSES:
        raise ValueError('Unknown status; choose one of: ' + ', '.join(STATUSES))
    with db:
        if category in ('theme', 'status'):  # single-valued, so a new review replaces the old one
            db.execute('DELETE FROM annotations WHERE post_id=? AND category=?', (post_id, category))
        db.execute('INSERT OR REPLACE INTO annotations VALUES(?,?,?,?,?)',
                   (post_id, category, value, provenance, now()))


def key_name(header):
    return re.sub(r'[^a-z0-9]+', '_', header.strip().lower()).strip('_')


def number(value):
    value = (value or '').strip().replace(',', '').rstrip('%')
    if not value:
        return None
    result = float(value)
    return int(result) if result.is_integer() else result


def calendar_day(value):
    value = value.strip()
    for fmt in ('%Y-%m-%d', '%a, %b %d, %Y', '%b %d, %Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        raise ValueError('Unrecognised date in analytics export: ' + value)


def import_analytics(db, path, exported_at=None, account=None):
    """Import an X analytics CSV: per-post rows by post ID or link, or account rows by date."""
    path = Path(path)
    with path.open(newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError('Analytics export has no rows')
    names = {h: key_name(h) for h in rows[0] if h}
    find = lambda *options: next((h for h, n in names.items() if n in options), None)
    id_col = find('post_id', 'tweet_id', 'id')
    link_col = find('post_link', 'tweet_permalink', 'permalink', 'link', 'url')
    date_col = find('date', 'day')
    numeric = []
    for h in names:
        if h in (id_col, link_col, date_col):
            continue
        try:
            values = [number(r.get(h)) for r in rows]
        except ValueError:
            continue
        if any(v is not None for v in values):
            numeric.append(h)
    exported = exported_at or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
    source = 'x analytics export' + (' @' + account.lstrip('@').lower() if account else '')
    records = []  # validate every row before writing any
    for r in rows:
        metrics = {names[h]: number(r.get(h)) for h in numeric if number(r.get(h)) is not None}
        if id_col or link_col:
            post_id = (r.get(id_col) or '').strip() if id_col else ''
            if not post_id.isdigit():
                match = re.search(r'/status/(\d+)', r.get(link_col) or '') if link_col else None
                post_id = match[1] if match else ''
            if not post_id:
                raise ValueError('Row without a post ID or link; nothing imported')
            records.append((post_id, metrics))
        elif date_col:
            records.append((calendar_day(r[date_col]), metrics))
        else:
            raise ValueError('No post ID, post link or date column; nothing imported')
    with db:
        for key, metrics in records:
            if id_col or link_col:
                db.execute('INSERT OR REPLACE INTO analytics_exports VALUES(?,?,?,?)',
                           (key, exported, path.name, json.dumps(metrics)))
            else:
                for name, value in metrics.items():
                    db.execute('INSERT OR REPLACE INTO daily_signals VALUES(?,?,?,?,?,?)',
                               (key, 'x_' + name, value, source, path.name, now()))
    return {'kind': 'posts' if (id_col or link_col) else 'daily', 'rows': len(records), 'source': source,
            'metrics': [names[h] for h in numeric], 'exported_at': exported}


def prospects_add(db, rows):
    """Record discovered posts; an existing prospect keeps its status and triage."""
    for row in rows:
        if not str(row.get('post_id', '')).isdigit() or not row.get('text'):
            raise ValueError('Prospect without post ID or text; nothing added')
    added = 0
    with db:
        for row in rows:
            added += db.execute('''INSERT OR IGNORE INTO prospects(post_id,url,author,followers,text,created_at,query,found_at)
              VALUES(?,?,?,?,?,?,?,?)''', (str(row['post_id']), row.get('url'), (row.get('author') or '').lower(),
              row.get('followers'), row['text'], row.get('created_at'), row.get('query'), now())).rowcount
    return {'received': len(rows), 'new': added}


def prospects_list(db, status='new', limit=60):
    rows = []
    for row in db.execute('SELECT * FROM prospects WHERE status=? ORDER BY created_at DESC LIMIT ?', (status, limit)):
        item = dict(row)
        # Earlier contact: our replies under their posts, or an earlier prospect of theirs we answered.
        own = ','.join('?' * len(OWN)) or "''"
        item['contacted'] = db.execute(f'''SELECT COUNT(*) FROM posts p JOIN posts parent ON parent.id=p.parent_id
          WHERE p.author IN ({own}) AND parent.author=?''', (*OWN, item['author'])).fetchone()[0] + \
          db.execute("SELECT COUNT(*) FROM prospects WHERE author=? AND status='replied' AND post_id!=?",
                     (item['author'], item['post_id'])).fetchone()[0]
        rows.append(item)
    return rows


def prospect_mark(db, post_id, status, reason=None, reply_id=None):
    if reply_id is not None and not str(reply_id).isdigit():
        raise ValueError('Reply post IDs are digits')
    with db:
        changed = db.execute('UPDATE prospects SET status=?,reason=?,reply_id=?,updated_at=? WHERE post_id=?',
                             (status, reason, reply_id, now(), post_id)).rowcount
    if not changed:
        raise ValueError('Unknown prospect ' + post_id)


def prospect_stage(db, post_id, stage, note=None):
    if stage not in STAGES:
        raise ValueError('Unknown stage; choose one of: ' + ', '.join(STAGES))
    with db:
        changed = db.execute("UPDATE prospects SET stage=?,reason=COALESCE(?,reason),updated_at=? WHERE post_id=? AND status='replied'",
                             (stage, note, now(), post_id)).rowcount
    if not changed:
        raise ValueError('Stages apply to replied prospects only: ' + post_id)


def funnel(db):
    replied = db.execute("SELECT COUNT(*) FROM prospects WHERE status='replied'").fetchone()[0]
    counts = dict(db.execute("SELECT stage,COUNT(*) FROM prospects WHERE status='replied' AND stage IS NOT NULL GROUP BY stage").fetchall())
    reached = lambda s: sum(counts.get(x, 0) for x in STAGES[STAGES.index(s):])
    leads = [dict(r) for r in db.execute("SELECT author,stage,url,reason FROM prospects WHERE stage IS NOT NULL ORDER BY updated_at DESC")]
    return {'replied': replied, 'answered': reached('answered'), 'offered': reached('offered'), 'ordered': reached('ordered'), 'leads': leads}


# A reply moves draft -> approved (only on a person's explicit approval) -> posted or failed.
MOVES = {'approved': ('draft', 'failed'), 'rejected': ('draft', 'approved', 'failed'), 'draft': ('approved', 'failed'),
         'posted': ('approved',), 'failed': ('approved',)}


def draft_reply(db, prospect_id, text, untick=()):
    if not db.execute('SELECT 1 FROM prospects WHERE post_id=?', (prospect_id,)).fetchone():
        raise ValueError('Unknown prospect ' + prospect_id)
    if not text.strip():
        raise ValueError('Empty reply')
    with db:
        return db.execute('INSERT INTO replies(prospect_id,text,untick,created_at,updated_at) VALUES(?,?,?,?,?)',
                          (prospect_id, text.strip(), json.dumps([u.lstrip('@').lower() for u in untick]), now(), now())).lastrowid


def move_reply(db, reply, status, reply_id=None, note=None):
    row = db.execute('SELECT status,prospect_id FROM replies WHERE id=?', (reply,)).fetchone()
    if not row:
        raise ValueError(f'Unknown reply {reply}')
    if row[0] not in MOVES[status]:
        raise ValueError(f'Reply {reply} is {row[0]}; it cannot become {status}')
    with db:
        db.execute('UPDATE replies SET status=?,reply_id=COALESCE(?,reply_id),note=COALESCE(?,note),updated_at=?,'
                   "posted_at=CASE WHEN ?='posted' THEN ? ELSE posted_at END WHERE id=?",
                   (status, reply_id, note, now(), status, now(), reply))
    if status == 'posted':
        prospect_mark(db, row[1], 'replied', reply_id=reply_id)


def queue(db, status=None):
    where, params = ('WHERE r.status=?', (status,)) if status else ('', ())
    return [dict(r) for r in db.execute(f'''SELECT r.id,r.status,r.text,r.untick,r.reply_id,r.note,r.posted_at,
      p.post_id,p.url,p.author,p.followers,p.text AS their_post FROM replies r JOIN prospects p ON p.post_id=r.prospect_id
      {where} ORDER BY r.id''', params)]


def limits(db, current=None):
    """Posting allowance for today in the configured time zone."""
    current = current or datetime.now(timezone.utc)
    midnight = current.astimezone(LOCAL).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    posted = db.execute("SELECT COUNT(*),MAX(posted_at) FROM replies WHERE status='posted' AND posted_at>=?",
                        (midnight.isoformat(timespec='seconds'),)).fetchone()
    last = db.execute("SELECT MAX(posted_at) FROM replies WHERE status='posted'").fetchone()[0]
    wait = 0
    if last:
        wait = max(0, CONFIG['min_minutes_between_posts'] * 60 - (current - parse_time(last)).total_seconds())
    return {'posted_today': posted[0], 'daily_limit': CONFIG['daily_reply_limit'],
            'remaining_today': max(0, CONFIG['daily_reply_limit'] - posted[0]), 'wait_seconds': round(wait)}


def voice(db, limit=40, authors=None):
    """Your own recent writing, for the agent to match: replies and posts, links and mentions removed."""
    authors = tuple(authors or OWN)
    if not authors:
        raise ValueError('Set "accounts" in config.json or pass --author')
    marks = ','.join('?' * len(authors))
    samples = []
    for row in db.execute(f'''SELECT text,is_reply,created_at FROM posts WHERE author IN ({marks})
        AND text NOT LIKE 'RT @%' ORDER BY created_at DESC''', authors):
        text = re.sub(r'https?://\S+', '', re.sub(r'^(?:@\w+\s+)+', '', row['text'])).strip()
        if len(text) >= 60:
            samples.append({'kind': 'reply' if row['is_reply'] else 'post', 'at': row['created_at'][:10], 'text': text})
        if len(samples) >= limit:
            break
    return samples


def answers_seen(db, rows):
    """Match replies addressed to you with prospects you answered; the first answer moves them to 'answered'."""
    out = []
    for row in rows:
        author = (row.get('author') or '').lstrip('@').lower()
        lead = db.execute("SELECT post_id,stage FROM prospects WHERE author=? AND status='replied' ORDER BY updated_at DESC",
                          (author,)).fetchone()
        if lead and lead['stage'] is None:
            prospect_stage(db, lead['post_id'], 'answered', 'answered: ' + ' '.join(row['text'].split())[:120])
        out.append({**row, 'lead': dict(lead) if lead else None})
    return out


def signal(db, day, key, value, source, note=None):
    datetime.strptime(day, '%Y-%m-%d')
    if not key_name(key):
        raise ValueError('Signal key is empty')
    with db:
        db.execute('INSERT OR REPLACE INTO daily_signals VALUES(?,?,?,?,?,?)',
                   (day, key_name(key), float(value), source, note, now()))


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def at_age(observations, created, window):
    low, high, target = window
    best = None
    for observed, metrics in observations:
        age = (observed - created).total_seconds() / 3600
        if low <= age < high and metrics.get('views') is not None:
            if best is None or abs(age - target) < abs(best[0] - target):
                best = (age, metrics)
    return best[1] if best else None


def engagement(metrics):
    if not metrics or not metrics.get('views'):
        return None
    total = sum(metrics.get(k) or 0 for k in ('likes', 'replies', 'retweets', 'quotes', 'bookmarks'))
    return total / metrics['views']


def parent_size(followers):
    if followers is None:
        return 'unknown'
    return '100k+' if followers >= 100000 else '10k-100k' if followers >= 10000 else 'under 10k'


def features(db, row):
    source = json.loads(row['raw_json']).get('source_tweet') or {}
    kinds = {r[0] for r in db.execute('SELECT kind FROM relationships WHERE post_id=?', (row['id'],))}
    text = row['text']
    if 'repost' in kinds or text.startswith('RT @'):
        kind = 'repost'
    elif row['is_reply']:
        kind = 'reply'
    elif 'quote' in kinds:
        kind = 'quote'
    else:
        kind = 'original'
    media = (source.get('extendedEntities') or source.get('extended_entities') or {}).get('media') or []
    types = {m.get('type') for m in media}
    urls = [(u.get('expanded_url') or '') for u in ((source.get('entities') or {}).get('urls') or [])]
    x_link = any(re.search(r'//(x|twitter)\.com/\w+/status/', u) for u in urls)
    web_link = any(u and not re.search(r'//(x|twitter)\.com/', u) for u in urls)
    fmt = ('video' if types & {'video', 'animated_gif'} else 'image' if 'photo' in types
           else 'x post link' if x_link else 'web link' if web_link else 'text')
    parent = db.execute('SELECT author,raw_json FROM posts WHERE id=?', (row['parent_id'],)).fetchone() if row['parent_id'] else None
    followers = None
    if parent:
        followers = ((json.loads(parent['raw_json']).get('source_tweet') or {}).get('author') or {}).get('followers')
    if kind == 'reply':
        audience = 'own thread' if parent and parent['author'] in OWN + (row['author'],) else parent_size(followers)
    else:
        audience = 'own post'
    theme = db.execute("SELECT value FROM annotations WHERE post_id=? AND category='theme'", (row['id'],)).fetchone()
    body = re.sub(r'^(?:@\w+\s+)+', '', text)
    return {'kind': kind, 'format': fmt, 'site_link': web_link, 'audience': audience,
            'parent': parent['author'] if parent else None, 'parent_followers': followers,
            'theme': theme[0] if theme else 'untagged', 'hour_local': parse_time(row['created_at']).astimezone(LOCAL).hour,
            'competitors': any(re.search(r'\b' + re.escape(c) + r'\b', body, re.I) for c in CONFIG['competitors']),
            'price': bool(re.search(r'\$\s?\d', body)), 'length': len(body)}


def hour_band(hour):
    start = hour // 3 * 3
    return f'{start:02d}-{start + 3:02d} local'


def report(db, days=30, authors=None, current=None):
    authors = tuple(authors or OWN)
    if not authors:
        raise ValueError('Set "accounts" in config.json or pass --author')
    current = current or datetime.now(timezone.utc)
    since = current - timedelta(days=days)
    marks = ','.join('?' * len(authors))
    posts = []
    for row in db.execute(f'SELECT * FROM posts WHERE author IN ({marks}) AND created_at>=? ORDER BY created_at DESC',
                          (*authors, since.isoformat())):
        item = features(db, row)
        deleted = db.execute("SELECT 1 FROM annotations WHERE post_id=? AND category='status' AND value='deleted'",
                             (row['id'],)).fetchone()
        if item['kind'] == 'repost' or deleted:
            continue
        created = parse_time(row['created_at'])
        observations = [(parse_time(o), json.loads(m)) for o, m in db.execute(
            'SELECT observed_at,metrics_json FROM observations WHERE post_id=? ORDER BY observed_at', (row['id'],))]
        latest = next(((o, m) for o, m in reversed(observations) if m.get('views') is not None), None)
        export = db.execute('SELECT metrics_json FROM analytics_exports WHERE post_id=? ORDER BY exported_at DESC LIMIT 1',
                            (row['id'],)).fetchone()
        x = json.loads(export[0]) if export else {}
        item.update({'id': row['id'], 'url': row['url'], 'author': row['author'], 'created_at': row['created_at'],
                     'text': row['text'], 'day1': at_age(observations, created, WINDOWS['day1']),
                     'day7': at_age(observations, created, WINDOWS['day7']),
                     'latest': latest[1] if latest else None,
                     'latest_age_hours': round((latest[0] - created).total_seconds() / 3600) if latest else None,
                     'link_clicks': next((x[k] for k in ('url_clicks', 'link_clicks') if k in x), None),
                     'profile_visits': x.get('profile_visits'), 'new_follows': x.get('new_follows')})
        item['engagement'] = engagement(item['latest'])
        posts.append(item)
    groups = {}
    for dimension, value in [('theme', lambda p: p['theme']), ('kind', lambda p: p['kind']),
                             ('format', lambda p: p['format']), ('audience', lambda p: p['audience']),
                             ('posting time', lambda p: hour_band(p['hour_local'])),
                             ('names competitors', lambda p: 'yes' if p['competitors'] else 'no'),
                             ('states price', lambda p: 'yes' if p['price'] else 'no'),
                             ('links site', lambda p: 'yes' if p['site_link'] else 'no')]:
        buckets = {}
        for p in posts:
            buckets.setdefault(value(p), []).append(p)
        rows = []
        for name, members in buckets.items():
            views = [p['latest']['views'] for p in members if p['latest']]
            day1 = [p['day1']['views'] for p in members if p['day1']]
            rates = [p['engagement'] for p in members if p['engagement'] is not None]
            clicks = [p['link_clicks'] for p in members if p['link_clicks'] is not None]
            rows.append({'value': name, 'posts': len(members),
                         'median_views': statistics.median(views) if views else None,
                         'median_day1_views': statistics.median(day1) if day1 else None, 'day1_posts': len(day1),
                         'median_engagement': statistics.median(rates) if rates else None,
                         'bookmarks': sum((p['latest'] or {}).get('bookmarks') or 0 for p in members),
                         'link_clicks': sum(clicks) if clicks else None})
        groups[dimension] = sorted(rows, key=lambda r: -(r['median_views'] or 0))
    daily = {}
    for p in posts:
        day = parse_time(p['created_at']).astimezone(LOCAL).date().isoformat()
        entry = daily.setdefault(day, {'posts': 0, 'views': 0, 'signals': {}})
        entry['posts'] += 1
        entry['views'] += (p['latest'] or {}).get('views') or 0
    for day, key, value in db.execute('SELECT day,key,SUM(value) FROM daily_signals WHERE day>=? GROUP BY day,key',
                                      (since.astimezone(LOCAL).date().isoformat(),)):
        daily.setdefault(day, {'posts': 0, 'views': 0, 'signals': {}})['signals'][key] = value
    return {'generated_at': current.isoformat(timespec='seconds'), 'days': days, 'authors': list(authors),
            'posts': posts, 'groups': groups, 'daily': dict(sorted(daily.items(), reverse=True)),
            'notes': ['Day 1 = the observation 12-36 h after posting; day 7 = 6-8 days. Blank = no observation then.',
                      'Views and engagement are public counts from TwitterAPI.io; clicks appear after an X analytics import.',
                      'Small sample: reach depends heavily on the parent account and timing. Directional, not causal; '
                      'orders, not views, are the metric (see funnel).']}


def render(result, limit=15):
    def n(value, pct=False):
        if value is None:
            return '-'
        return f'{value * 100:.1f}%' if pct else f'{value:,.0f}'
    out = [f"X report: {', '.join(result['authors'])}, last {result['days']} days, "
           f"{len(result['posts'])} posts (reposts and deleted posts excluded), generated {result['generated_at']}"]
    out += ['  ' + note for note in result['notes']]
    out += ['', f'TOP {limit} POSTS BY VIEWS',
            f"{'date':10} {'account':12} {'kind':8} {'theme':21} {'audience':14} {'day1':>6} {'day7':>6} {'views':>7} {'eng':>6} {'bkm':>4} {'clicks':>6}  text"]
    ranked = sorted(result['posts'], key=lambda p: -((p['latest'] or {}).get('views') or 0))
    for p in ranked[:limit]:
        text = re.sub(r'\s+', ' ', re.sub(r'^(?:@\w+\s+)+', '', p['text']))[:48]
        out.append(f"{p['created_at'][:10]:10} {p['author']:12} {p['kind']:8} {p['theme'][:21]:21} {p['audience']:14} "
                   f"{n((p['day1'] or {}).get('views')):>6} {n((p['day7'] or {}).get('views')):>6} "
                   f"{n((p['latest'] or {}).get('views')):>7} {n(p['engagement'], True):>6} "
                   f"{n((p['latest'] or {}).get('bookmarks')):>4} {n(p['link_clicks']):>6}  {text}")
    for dimension, rows in result['groups'].items():
        out += ['', f'BY {dimension.upper()}',
                f"{'':24} {'posts':>5} {'med views':>9} {'med day1':>9} {'day1 n':>6} {'med eng':>7} {'bkm':>4} {'clicks':>6}"]
        for r in rows:
            out.append(f"{str(r['value'])[:24]:24} {r['posts']:>5} {n(r['median_views']):>9} {n(r['median_day1_views']):>9} "
                       f"{r['day1_posts']:>6} {n(r['median_engagement'], True):>7} {r['bookmarks']:>4} {n(r['link_clicks']):>6}")
    # Business signals plus the account numbers that bear on reach; the rest stay in --json.
    keys = sorted({k for d in result['daily'].values() for k in d['signals']
                   if not k.startswith('x_') or k in ('x_impressions', 'x_profile_visits', 'x_new_follows')})
    out += ['', 'BY DAY (local): posts made that day, their views to date, and recorded signals',
            f"{'day':10} {'posts':>5} {'views':>7}" + ''.join(f' {k[:14]:>14}' for k in keys)]
    for day, d in result['daily'].items():
        out.append(f"{day:10} {d['posts']:>5} {n(d['views']):>7}" + ''.join(f" {n(d['signals'].get(k)):>14}" for k in keys))
    return '\n'.join(out)


def prune_backups(folder, current=None, keep_days=3, dry_run=False):
    """Keep every backup from the last keep_days days, then the newest backup of each earlier UTC day."""
    current = current or datetime.now(timezone.utc)
    dated = []
    for path in Path(folder).glob('content-*.sqlite3'):
        try:
            taken = datetime.strptime(path.name[8:-8], '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dated.append((taken, path))
    kept_days, removed = set(), []
    for taken, path in sorted(dated, reverse=True):
        if current - taken < timedelta(days=keep_days):
            continue
        if taken.date() in kept_days:
            removed.append(path)
            if not dry_run:
                path.unlink()
        else:
            kept_days.add(taken.date())
    return [str(p) for p in removed]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest='cmd', required=True)
    imp = sub.add_parser('import', help='Read normalized connector JSON from file or stdin')
    imp.add_argument('file', nargs='?', default='-')
    imp.add_argument('--cursor')
    imp.add_argument('--observed-at')
    sub.add_parser('status')
    sub.add_parser('check')
    ids = sub.add_parser('ids')
    ids.add_argument('--author', default=OWN[0] if OWN else None)
    search = sub.add_parser('search')
    search.add_argument('text', nargs='?', default='')
    search.add_argument('--author', default=OWN[0] if OWN else None)
    search.add_argument('--to', help='Parent author, or a leading reply mention when parent is unavailable')
    search.add_argument('--limit', type=int, default=20)
    search.add_argument('--topic')
    missing = sub.add_parser('missing-parents')
    missing.add_argument('--author', default=OWN[0] if OWN else None)
    missing.add_argument('--limit', type=int, default=100)
    fail = sub.add_parser('context-failed')
    fail.add_argument('id')
    fail.add_argument('reason')
    sub.add_parser('backup')
    export = sub.add_parser('export')
    export.add_argument('destination')
    tag_cmd = sub.add_parser('tag')
    tag_cmd.add_argument('id')
    tag_cmd.add_argument('category', choices=['topic','theme','status','angle','asset','campaign','note'])
    tag_cmd.add_argument('value')
    tag_cmd.add_argument('--provenance', required=True)
    draft = sub.add_parser('draft', help='Queue a reply to a prospect; text from a file or stdin')
    draft.add_argument('prospect')
    draft.add_argument('file', nargs='?', default='-')
    draft.add_argument('--untick', action='append', default=[], help='Account to remove from the reply recipients')
    q = sub.add_parser('queue', help='Show queued replies')
    q.add_argument('--status', choices=['draft', 'approved', 'posted', 'rejected', 'failed'])
    q.add_argument('--json', action='store_true')
    for verb in ('approve', 'reject', 'redraft'):
        v = sub.add_parser(verb, help=f'{verb.capitalize()} queued replies by number')
        v.add_argument('ids', nargs='+', type=int)
    done = sub.add_parser('posted', help='Record a posted reply (used by post.mjs)')
    done.add_argument('id', type=int)
    done.add_argument('--reply')
    bad = sub.add_parser('failed', help='Record a reply that could not be posted')
    bad.add_argument('id', type=int)
    bad.add_argument('--note', required=True)
    sub.add_parser('limits', help="Today's posting allowance as JSON")
    vc = sub.add_parser('voice', help='Your own recent posts and replies, for matching your style')
    vc.add_argument('--limit', type=int, default=40)
    sub.add_parser('answers', help='Match replies to you (JSON on stdin) with your leads')
    rep = sub.add_parser('report', help='Rank own posts and compare themes, formats and audiences')
    rep.add_argument('--days', type=int, default=30)
    rep.add_argument('--author', action='append', help='Repeatable; defaults to both own accounts')
    rep.add_argument('--limit', type=int, default=15)
    rep.add_argument('--json', action='store_true')
    analytics = sub.add_parser('import-analytics', help='Import an X analytics CSV export')
    analytics.add_argument('file')
    analytics.add_argument('--exported-at', help='ISO time of the export; defaults to the file time')
    analytics.add_argument('--account', help='X account the export belongs to')
    sig = sub.add_parser('signal', help='Record a daily business number, such as purchases')
    sig.add_argument('day', help='YYYY-MM-DD')
    sig.add_argument('key')
    sig.add_argument('value', type=float)
    sig.add_argument('--source', required=True)
    sig.add_argument('--note')
    pros = sub.add_parser('prospects', help='Add discovered posts (JSON on stdin) or list them for triage')
    pros.add_argument('action', choices=['add', 'list'])
    pros.add_argument('--status', choices=['new', 'skipped', 'replied'], default='new')
    pros.add_argument('--limit', type=int, default=60)
    mark = sub.add_parser('prospect', help='Record triage: skipped with a reason, or replied with the reply ID')
    mark.add_argument('id')
    mark.add_argument('status', choices=['skipped', 'replied', 'new'])
    mark.add_argument('--reason')
    mark.add_argument('--reply')
    stage = sub.add_parser('stage', help='Move a replied prospect along: answered, offered, ordered')
    stage.add_argument('id')
    stage.add_argument('stage', choices=list(STAGES))
    stage.add_argument('--note')
    sub.add_parser('funnel', help='Replied prospects by sales stage; orders are the metric')
    prune = sub.add_parser('prune-backups', help='Keep recent backups and one per earlier day')
    prune.add_argument('--keep-days', type=int, default=3)
    prune.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.cmd in ('ids', 'search', 'missing-parents') and not args.author:
        parser.error('set "accounts" in config.json or pass --author')
    db = connect(args.db)
    if args.cmd == 'import':
        value = json.load(sys.stdin) if args.file == '-' else json.loads(Path(args.file).read_text())
        print(json.dumps(ingest(db, value, args.cursor, args.observed_at)))
    elif args.cmd == 'check':
        check(db)
        print('ok')
    elif args.cmd == 'ids':
        print(json.dumps([dict(r) for r in db.execute('SELECT id,created_at FROM posts WHERE author=? ORDER BY created_at DESC',(args.author.lower(),))]))
    elif args.cmd == 'status':
        coverage=[]
        for author in OWN:
            counts=set()
            for r in db.execute('SELECT raw_json FROM posts WHERE author=?',(author,)):
                value=(json.loads(r[0]).get('source_tweet') or {}).get('author',{}).get('statusesCount')
                if value is not None: counts.add(value)
            coverage.append({'author':author,'archived':db.execute('SELECT COUNT(*) FROM posts WHERE author=?',(author,)).fetchone()[0],
                             'profile_status_counts_observed':sorted(counts),
                             'note':'Profile counts can include reposts and unavailable items; do not infer full coverage.'})
        result = {'accounts': [dict(r) for r in db.execute('''SELECT author,COUNT(*) posts,
          SUM(is_reply) replies,MIN(created_at) oldest,MAX(created_at) newest FROM posts GROUP BY author''')],
          'imports': db.execute('SELECT COUNT(*) FROM imports').fetchone()[0],
          'context_failures': [dict(r) for r in db.execute('SELECT * FROM context_attempts')],
          'account_coverage':coverage,
          'latest_pages': [dict(r) for r in db.execute('''SELECT command,query,count,
            has_next_page,next_cursor,observed_at FROM imports ORDER BY id DESC LIMIT 5''')],
          'coverage': 'Observed API results only. Pagination exhaustion is not proof of all-time completeness.'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == 'search':
        rows = db.execute('''SELECT p.*, parent.author AS parent_author,parent.text AS parent_text
          FROM posts p LEFT JOIN posts parent ON parent.id=p.parent_id
          WHERE p.author=? AND instr(lower(p.text),lower(?))>0 ORDER BY p.created_at DESC''',
          (args.author.lstrip('@').lower(), args.text))
        results=[]
        for row in rows:
            item=dict(row)
            if args.topic and not db.execute("SELECT 1 FROM annotations WHERE post_id=? AND category='topic' AND value=?",(item['id'],args.topic)).fetchone():
                continue
            if args.to:
                recipient=args.to.lstrip('@').lower()
                leading = re.match(r'^(?:@\w+\s+)+', item['text'])
                mentions = re.findall(r'@(\w+)', leading[0].lower()) if leading else []
                if item['parent_author'] != recipient and not (item['is_reply'] and recipient in mentions):
                    continue
            raw=json.loads(item.pop('raw_json'))
            source=raw.get('source_tweet') or {}
            item['links']=(source.get('entities') or {}).get('urls',[])
            item['media']=(source.get('extendedEntities') or source.get('extended_entities') or {}).get('media',[])
            item['relationships']=[dict(r) for r in db.execute('SELECT kind,target_id FROM relationships WHERE post_id=?',(item['id'],))]
            observation=db.execute('SELECT observed_at,metrics_json FROM observations WHERE post_id=? ORDER BY observed_at DESC LIMIT 1',(item['id'],)).fetchone()
            item['latest_observation']={'observed_at':observation[0],'metrics':json.loads(observation[1])} if observation else None
            item['annotations']=[dict(r) for r in db.execute('SELECT category,value,provenance FROM annotations WHERE post_id=?',(item['id'],))]
            results.append(item)
            if len(results)>=args.limit: break
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.cmd == 'missing-parents':
        print(json.dumps([r[0] for r in db.execute('''SELECT DISTINCT p.parent_id FROM posts p
          LEFT JOIN posts q ON q.id=p.parent_id LEFT JOIN context_attempts a ON a.post_id=p.parent_id
          WHERE p.author=? AND p.parent_id IS NOT NULL AND q.id IS NULL AND a.post_id IS NULL
          ORDER BY p.created_at DESC LIMIT ?''',(args.author.lower(),args.limit))]))
    elif args.cmd == 'context-failed':
        with db: db.execute('INSERT OR REPLACE INTO context_attempts VALUES(?,?,?)',(args.id,now(),args.reason))
    elif args.cmd == 'backup':
        folder=Path(args.db).parent/'backups'
        folder.mkdir(exist_ok=True)
        path=folder/('content-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
        with sqlite3.connect(path) as target: db.backup(target)
        print(path)
    elif args.cmd == 'export':
        path=Path(args.destination)
        path.mkdir(parents=True,exist_ok=True)
        for table in ('posts','observations','annotations','replies','imports','context_attempts','post_versions',
                      'relationships','analytics_exports','daily_signals','prospects'):
            with (path/(table+'.jsonl')).open('w') as f:
                for row in db.execute('SELECT * FROM '+table):
                    f.write(json.dumps(dict(row),ensure_ascii=False)+'\n')
        print(path)
    elif args.cmd == 'tag':
        tag(db, args.id, args.category, args.value, args.provenance)
    elif args.cmd == 'draft':
        text = sys.stdin.read() if args.file == '-' else Path(args.file).read_text()
        print(draft_reply(db, args.prospect, text, args.untick))
    elif args.cmd == 'queue':
        rows = queue(db, args.status)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        for r in [] if args.json else rows:
            untick = json.loads(r['untick'])
            print(f"#{r['id']} [{r['status']}] @{r['author']} ({r['followers']}) {r['url']}"
                  + (f"  untick: {', '.join('@' + u for u in untick)}" if untick else ''))
            print('    they said: ' + ' '.join(r['their_post'].split())[:200])
            print('    reply: ' + r['text'].replace('\n', '\n           ') + '\n')
    elif args.cmd in ('approve', 'reject', 'redraft'):
        for reply in args.ids:
            move_reply(db, reply, {'approve': 'approved', 'reject': 'rejected', 'redraft': 'draft'}[args.cmd])
    elif args.cmd == 'posted':
        move_reply(db, args.id, 'posted', reply_id=args.reply)
    elif args.cmd == 'failed':
        move_reply(db, args.id, 'failed', note=args.note)
    elif args.cmd == 'limits':
        print(json.dumps(limits(db)))
    elif args.cmd == 'voice':
        for s in voice(db, args.limit):
            print(f"[{s['kind']} {s['at']}] {s['text']}\n")
    elif args.cmd == 'answers':
        print(json.dumps(answers_seen(db, json.load(sys.stdin)), ensure_ascii=False))
    elif args.cmd == 'report':
        result = report(db, args.days, tuple(a.lstrip('@').lower() for a in args.author) if args.author else None)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result, args.limit))
    elif args.cmd == 'import-analytics':
        print(json.dumps(import_analytics(db, args.file, args.exported_at, args.account)))
    elif args.cmd == 'signal':
        signal(db, args.day, args.key, args.value, args.source, args.note)
    elif args.cmd == 'prospects':
        if args.action == 'add':
            print(json.dumps(prospects_add(db, json.load(sys.stdin))))
        else:
            for p in prospects_list(db, args.status, args.limit):
                print(f"{(p['created_at'] or '')[:16]}  @{p['author']} ({p['followers']})  contacted:{p['contacted']}  "
                      f"[{p['query']}]  {p['url']}\n    {' '.join(p['text'].split())[:260]}")
    elif args.cmd == 'prospect':
        prospect_mark(db, args.id, args.status, args.reason, args.reply)
    elif args.cmd == 'stage':
        prospect_stage(db, args.id, args.stage, args.note)
    elif args.cmd == 'funnel':
        f = funnel(db)
        print(f"replied {f['replied']}  ->  answered {f['answered']}  ->  offered {f['offered']}  ->  ordered {f['ordered']}")
        for lead in f['leads']:
            print(f"  {lead['stage']:9} @{lead['author']}  {lead['url']}  {lead['reason'] or ''}")
    elif args.cmd == 'prune-backups':
        print(json.dumps(prune_backups(Path(args.db).parent / 'backups', keep_days=args.keep_days, dry_run=args.dry_run)))
    db.close()


if __name__ == '__main__':
    main()
