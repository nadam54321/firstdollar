// The signed-in browser the posting and analytics scripts use: a dedicated profile, or a running Chrome over CDP.
import { join } from 'node:path';
import { chromium } from 'playwright-core';
import { config, root } from './config.mjs';

export async function openBrowser(option) {
  const cdp = option('--cdp', config.browser?.cdp);
  if (cdp) {
    const b = await chromium.connectOverCDP(cdp);
    return { context: b.contexts()[0], close: () => b.close() };
  }
  const profile = option('--profile', config.browser?.profile || join(root, 'data/browser-profile'));
  const context = await chromium.launchPersistentContext(profile, {
    channel: config.browser?.channel ?? 'chrome', headless: config.browser?.headless ?? false,
    acceptDownloads: true, viewport: { width: 1280, height: 900 } });
  return { context, close: () => context.close() };
}
