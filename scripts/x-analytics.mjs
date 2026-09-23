// Download X's analytics exports (posts and account overview) in a signed-in browser. Needs X Premium.
import { join } from 'node:path';

export class Stop extends Error {}
const PREMIUM = /get premium|upgrade to premium|subscribe to premium|premium to see|available with premium/i;

async function grab(page, dir, trigger, timeout) {
  const [download] = await Promise.all([page.waitForEvent('download', { timeout }), trigger()]);
  const file = join(dir, download.suggestedFilename());
  await download.saveAs(file);
  return file;
}

async function clickFirst(candidates) {
  for (const c of candidates) if (await c.first().isVisible().catch(() => false)) { await c.first().click(); return true; }
  return false;
}

export async function downloadExports(page, { url = 'https://x.com/i/account_analytics', dir, timeout = 20000 } = {}) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  const exportButton = page.getByRole('button', { name: /export/i });
  try { await exportButton.first().waitFor({ state: 'visible', timeout }); } catch {
    const text = await page.evaluate(() => document.body.innerText.slice(0, 4000));
    if (/log in to x|sign in to x/i.test(text)) throw new Stop('Not signed in to X in this browser');
    if (PREMIUM.test(text)) throw new Stop('X shows analytics exports only to Premium accounts');
    throw new Stop('No Export button found on the analytics page');
  }
  const files = [];
  for (const kind of [/^(content|posts)/i, /^(overview|account)/i]) {
    const file = await grab(page, dir, async () => {
      await exportButton.first().click();
      const chose = await clickFirst([page.getByRole('menuitem', { name: kind }), page.getByRole('button', { name: kind }),
        page.getByRole('option', { name: kind }), page.getByText(kind)]);
      if (!chose) throw new Stop('Export menu did not offer ' + kind.source);
      await clickFirst([page.getByRole('button', { name: /^(download|export)$/i })]);
    }, timeout);
    files.push(file);
  }
  return files;
}

// When X's page differs: the person clicks Export; every download is kept until the tab closes.
export async function collectManually(page, dir, { url = 'https://x.com/i/account_analytics' } = {}) {
  const files = [];
  page.on('download', async d => { const f = join(dir, d.suggestedFilename()); await d.saveAs(f); files.push(f); });
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForEvent('close', { timeout: 0 });
  return files;
}
