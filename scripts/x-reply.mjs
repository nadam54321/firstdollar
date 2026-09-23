// Post one approved reply on x.com in a signed-in browser page, or stop without posting.
// Every step is checked; any doubt ends in a thrown Stop and nothing is sent.

export class Stop extends Error {}

const BOX = '[data-testid="tweetTextarea_0"]';
const SEND = '[data-testid="tweetButtonInline"]';
const TOAST = '[data-testid="toast"]';
const BLOCKERS = /log in to x|sign in to x|verify you are human|are you a robot|account is (temporarily )?locked|suspended|rate limit|try again later/i;

export const normalize = text => text.replace(/\r/g, '').replace(/[ \t]+\n/g, '\n').replace(/\n\s*\n+/g, '\n\n').trim();

async function focused(page) {
  return page.evaluate(sel => {
    const box = document.querySelector(sel);
    return !!box && !!document.activeElement && (box === document.activeElement || box.contains(document.activeElement));
  }, BOX);
}

async function blocked(page) {
  const text = await page.evaluate(() => document.body.innerText.slice(0, 5000));
  return BLOCKERS.test(text) ? text.match(BLOCKERS)[0] : null;
}

async function recipients(page) {
  return page.evaluate(() => {
    const line = [...document.querySelectorAll('div, span')].map(e => e.innerText || '')
      .find(t => /^Replying to\b/.test(t) && t.length < 200);
    return line ? [...line.matchAll(/@(\w+)/g)].map(m => m[1].toLowerCase()) : [];
  });
}

async function untick(page, handles) {
  if (!handles.length) return;
  const present = await recipients(page);
  const remove = handles.filter(h => present.includes(h));
  if (!remove.length) return;
  await page.getByText(/^Replying to/).first().locator('a, [role="link"], [role="button"]').first().click();
  const dialog = page.locator('[role="dialog"]').last();
  await dialog.waitFor({ state: 'visible', timeout: 10000 });
  for (const handle of remove) {
    // The handle is its own text node in the row; its nearest ancestor holding a checkbox is the row.
    const label = dialog.getByText(new RegExp('^@' + handle + '$', 'i')).first();
    const row = label.locator('xpath=ancestor::*[.//*[@role="checkbox"] or .//input[@type="checkbox"]][1]');
    await row.locator('[role="checkbox"], input[type="checkbox"]').first().click({ timeout: 10000 });
  }
  await dialog.getByRole('button', { name: /^Done$/ }).click();
  await dialog.waitFor({ state: 'hidden', timeout: 10000 });
  const left = await recipients(page);
  const stuck = remove.filter(h => left.includes(h));
  if (stuck.length) throw new Stop('Could not remove recipients: @' + stuck.join(', @'));
}

// reply: { url, text, untick: [handles] }. Returns { posted, replyUrl } or throws Stop.
export async function postReply(page, reply, { dryRun = false, timeout = 20000 } = {}) {
  await page.goto(reply.url, { waitUntil: 'domcontentloaded' });
  const box = page.locator(BOX).first();
  try { await box.waitFor({ state: 'visible', timeout }); } catch {
    throw new Stop((await blocked(page)) || 'Reply box not found; are you signed in to X in this browser?');
  }
  const blocker = await blocked(page);
  if (blocker) throw new Stop('X shows: ' + blocker);

  await box.click();
  if (!(await focused(page))) throw new Stop('The reply box did not take focus; nothing typed');
  const lines = normalize(reply.text).split('\n');
  for (const [i, line] of lines.entries()) {
    if (i) {
      if (!(await focused(page))) throw new Stop('Focus left the reply box while typing; not posted');
      await page.keyboard.press('Enter');
    }
    // insertText sends no key events, so X's single-key shortcuts can never fire.
    if (line) await page.keyboard.insertText(line);
  }
  const typed = normalize(await box.innerText());
  if (typed !== normalize(reply.text)) throw new Stop('Text in the reply box does not match the approved reply; not posted');

  await untick(page, reply.untick || []);
  if (dryRun) return { posted: false, replyUrl: null };

  const send = page.locator(SEND).first();
  if (await send.isDisabled()) throw new Stop('Reply button is disabled; not posted');
  await send.click();
  const toast = page.locator(TOAST).filter({ hasText: /sent/i }).first();
  try { await toast.waitFor({ state: 'visible', timeout: 15000 }); } catch {
    throw new Stop('No "sent" confirmation from X; check the thread by hand before anything else is posted');
  }
  const href = await toast.locator('a[href*="/status/"]').first().getAttribute('href').catch(() => null);
  return { posted: true, replyUrl: href ? new URL(href, 'https://x.com').href : null };
}
