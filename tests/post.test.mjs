import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { chromium } from 'playwright-core';
import { postReply, normalize, Stop } from '../scripts/x-reply.mjs';

const mock = mode => pathToFileURL(join(import.meta.dirname, 'fixtures/x-mock.html')).href + (mode ? '?mode=' + mode : '');
const text = 'your weekly cap is the problem, not you.\n\nif you already pay for Claude, run it on a computer that stays on!';
let browser, page;
before(async () => { browser = await chromium.launch({ channel: process.env.FIRSTDOLLAR_CHANNEL || 'chrome', headless: true }); });
after(async () => { await browser?.close(); });

test('posts the approved text to the right people and fires no shortcuts', async () => {
  page = await browser.newPage();
  const result = await postReply(page, { url: mock(), text, untick: ['bigbrand'] });
  assert.equal(result.posted, true);
  assert.match(result.replyUrl, /\/status\/12345$/);
  assert.equal(normalize(await page.evaluate(() => window.sentText)), normalize(text));
  assert.equal(await page.locator('#rt-link').innerText(), '@Bob');
  assert.equal(await page.evaluate(() => window.shortcuts), '');
});

test('a dry run fills the box and never presses Reply', async () => {
  page = await browser.newPage();
  const result = await postReply(page, { url: mock(), text, untick: [] }, { dryRun: true });
  assert.equal(result.posted, false);
  assert.equal(await page.evaluate(() => window.sentText), undefined);
  assert.equal(normalize(await page.locator('[data-testid="tweetTextarea_0"]').innerText()), normalize(text));
});

for (const [mode, reason] of [['login', /log in to x/i], ['nofocus', /did not take focus/], ['mangle', /does not match/]]) {
  test(`stops without posting when ${mode}`, async () => {
    page = await browser.newPage();
    await assert.rejects(postReply(page, { url: mock(mode), text, untick: [] }, { timeout: 3000 }),
      e => e instanceof Stop && reason.test(e.message));
    assert.equal(await page.evaluate(() => window.sentText), undefined);
  });
}
