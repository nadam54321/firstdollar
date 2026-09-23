#!/bin/sh
# The scheduled part of the daily loop: archive your posts, refresh recent numbers, find new
# prospects and check who answered. Read-only on X; drafting and posting wait for you.
set -u
cd "$(dirname "$0")/.." || exit 1
for account in $(node -e "import('./scripts/config.mjs').then(m => console.log(m.own.join(' ')))"); do
  node scripts/sync.mjs --account "$account" < /dev/null
  node scripts/sync.mjs --account "$account" --refresh --no-backup < /dev/null
done
node scripts/discover.mjs --hours 48 --pages 2 < /dev/null
node scripts/answers.mjs < /dev/null
python3 scripts/archive.py prune-backups > /dev/null
