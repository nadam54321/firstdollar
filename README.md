# firstdollar

Find people on X who are already asking for what you built, answer them in your own words, post
the replies you approve, and follow each person to a paid order.

People post their needs every day: "is there a tool that…", "X got too expensive", "looking for
an alternative to Y". A founder whose product fits wins customers by answering them, but those
posts are scattered, gone within a day or two, and finding them by hand takes hours. Reply bots
read as spam and get accounts restricted.

firstdollar splits the work. Scripts search, remember, post and count. Your own AI agent (Claude
Code, Codex, Grok Build, OpenCode or any other) reads, skips and drafts by following
[AGENTS.md](AGENTS.md), in a voice learned from your past posts. You approve every reply.

## What you get

- **Discovery**: searches you define, one per kind of need, over the last 48 hours of X.
- **Your voice**: your own posts are archived, so drafts sound like you and not like marketing.
- **A prospect log**: every person found, why they were skipped, when you replied, and whether
  they answered, got an offer or ordered. Nobody gets pitched twice.
- **A reply queue**: drafts wait for your approval by number.
- **Careful posting**: approved replies are posted in your own signed-in browser, one at a time,
  within a daily limit, with the text read back before sending and a hard stop at any doubt.
- **Follow-ups**: who answered you, matched with the people you replied to.
- **A report and a funnel**: which replies led to answers and orders, with X's own analytics
  export downloaded for you if your account has Premium.

Everything stays in a local SQLite file. Nothing is posted without your approval.

## Setup

Needs Node 22+, Python 3.9+, Google Chrome, a [TwitterAPI.io](https://twitterapi.io) key for
reading X, and any AI coding agent that reads `AGENTS.md`.

```sh
npm install
export TWITTERAPI_IO_API_KEY=...        # keep it in your shell profile, never in the repo
node scripts/doctor.mjs                 # says what is missing
```

Then ask your agent: *"Set up firstdollar for my product."* It fills `config.json` from
`config.example.json`, reads your product page, drafts `brand.md` (facts, price, proof, what not
to say) for your approval, learns your voice from your posts, and opens a browser for you to sign
in to X once.

After that, each day: *"Run today's firstdollar loop."* It finds, triages and drafts, shows you
the queue, and posts the numbers you approve.

## Running it every day

The loop works best on a computer that stays on. `scripts/morning.sh` does the reading half
(your posts, new prospects, who answered) and can run from cron; drafting and posting wait for
you. I run mine on a [Wrkr](https://wrkr.dev) cloud computer so it keeps going with my laptop
closed.

## Commands

| Command | Does |
| --- | --- |
| `node scripts/doctor.mjs` | Check the setup |
| `node scripts/sync.mjs [--refresh] [--account HANDLE]` | Archive your own posts; refresh recent metrics |
| `python3 scripts/archive.py voice` | Your recent posts, for matching your style |
| `node scripts/discover.mjs [--hours 48] [--pages 2]` | Find new prospects with your searches |
| `python3 scripts/archive.py prospects list` | New prospects, with earlier contact flagged |
| `python3 scripts/archive.py prospect ID skipped --reason ...` | Record a skip |
| `python3 scripts/archive.py draft ID [--untick @account]` | Queue a reply (text on stdin) |
| `python3 scripts/archive.py queue [--status draft]` | Review the queue |
| `python3 scripts/archive.py approve N...` | Approve replies by number |
| `node scripts/post.mjs [--login] [--dry-run] [--only N] [--cdp URL]` | Post approved replies |
| `node scripts/answers.mjs [--hours 72]` | Who replied to you; leads first |
| `python3 scripts/archive.py stage ID answered\|offered\|ordered` | Move a lead along |
| `python3 scripts/archive.py funnel` | Replied → answered → offered → ordered |
| `python3 scripts/archive.py report --days 7` | What earned attention, compared fairly by age |
| `node scripts/analytics.mjs [--manual]` | Download and import X's analytics exports (Premium) |
| `scripts/morning.sh` | The scheduled reading half of the loop |

## Guardrails

X restricts automated and unsolicited replies. firstdollar posts only replies a person approved
one by one, caps them per day, spaces them out, and stops on any warning. You are still
responsible for what your account posts; read X's rules and keep the volume human.

Posting and analytics downloads drive x.com's own pages, so they can break when X changes them.
They stop instead of guessing; `post.mjs --dry-run` and `analytics.mjs --manual` let you check
by hand.

## Tests

```sh
npm test    # archive, queue, limits and funnel; posting and exports against local mocks of X
```

## License

MIT
