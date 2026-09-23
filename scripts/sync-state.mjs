// Validate before importing a page or replacing the last resumable checkpoint.
export function pagination(raw, cursor, seen) {
  const body = raw.data || raw;
  const more = raw.has_next_page ?? body.has_next_page;
  const next = raw.next_cursor ?? body.next_cursor ?? null;
  if (typeof more !== 'boolean') throw Error('Pagination flag missing; coverage unknown');
  if (more && (typeof next !== 'string' || !next || next === cursor || seen.has(next))) {
    throw Error('Repeated/missing cursor; backfill incomplete');
  }
  return { exhausted: !more, next: more ? next : null };
}

// Own posts young enough for their day-7 metrics window still need fresh observations.
export function refreshIds(rows, days, now = Date.now()) {
  if (!Number.isFinite(days) || days <= 0 || days > 60) throw Error('Invalid refresh window');
  const cutoff = now - days * 86400000;
  return rows.filter(r => Date.parse(r.created_at) >= cutoff).map(r => r.id);
}
