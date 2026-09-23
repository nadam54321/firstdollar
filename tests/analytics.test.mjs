import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, basename } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright-core';
import { downloadExports, Stop } from '../scripts/x-analytics.mjs';

const mock = mode => pathToFileURL(join(import.meta.dirname, 'fixtures/x-analytics-mock.html')).href + (mode ? '?mode=' + mode : '');
let browser, context;
before(async () => {
  browser = await chromium.launch({ channel: process.env.FIRSTDOLLAR_CHANNEL || 'chrome', headless: true });
  context = await browser.newContext({ acceptDownloads: true });
});
after(async () => { await browser?.close(); });

test('downloads both exports through the Export menu', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'earshot-'));
  const files = await downloadExports(await context.newPage(), { url: mock(), dir, timeout: 5000 });
  assert.deepEqual(files.map(f => basename(f)), ['account_analytics_content_2026-09-17_2026-09-23.csv', 'account_overview_analytics.csv']);
  assert.match(readFileSync(files[0], 'utf8'), /Post id,Impressions/);
});

test('says plainly when the account is not Premium', async () => {
  await assert.rejects(downloadExports(await context.newPage(), { url: mock('nopremium'), dir: tmpdir(), timeout: 2000 }),
    e => e instanceof Stop && /Premium/.test(e.message));
});
