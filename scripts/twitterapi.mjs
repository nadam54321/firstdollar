// Read-only TwitterAPI.io access. Set TWITTERAPI_IO_API_KEY in your environment (never in this repo).
const key = process.env.TWITTERAPI_IO_API_KEY;
if (!key) throw Error('Set TWITTERAPI_IO_API_KEY (https://twitterapi.io) in your environment');

export async function request(endpoint, params) {
  const url = new URL('https://api.twitterapi.io' + endpoint);
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await fetch(url, { headers: { 'X-API-Key': key, Accept: 'application/json' }, signal: AbortSignal.timeout(45000) });
    if ((res.status === 429 || res.status >= 500) && attempt < 2) { await new Promise(r => setTimeout(r, 1000 * (attempt + 1))); continue; }
    if (!res.ok) throw Error('TwitterAPI.io HTTP ' + res.status);
    const json = await res.json();
    if (json.status === 'error' || json.error) throw Error('TwitterAPI.io returned an error');
    return json;
  }
}

export function pageTweets(raw) {
  if (Array.isArray(raw.tweets)) return raw.tweets;
  if (Array.isArray(raw.data?.tweets)) return raw.data.tweets;
  throw Error('Unknown response shape; refusing to mark sync complete');
}
