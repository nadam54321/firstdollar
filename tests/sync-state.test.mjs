import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pagination, refreshIds } from '../scripts/sync-state.mjs';

test('nested and top-level pagination preserve resumable cursors', () => {
  assert.deepEqual(pagination({data:{has_next_page:true,next_cursor:'b'}},'a',new Set()),
    {exhausted:false,next:'b'});
  assert.deepEqual(pagination({has_next_page:false,next_cursor:'a'},'a',new Set(['a'])),
    {exhausted:true,next:null});
});

test('invalid pages cannot advance a checkpoint', () => {
  for (const raw of [{}, {has_next_page:true}, {has_next_page:true,next_cursor:42},
    {has_next_page:true,next_cursor:'a'}, {has_next_page:true,next_cursor:'old'}]) {
    assert.throws(() => pagination(raw,'a',new Set(['old'])));
  }
});

test('refresh selects only posts inside the window', () => {
  const now = Date.parse('2026-09-23T00:00:00Z');
  const rows = [{id:'3',created_at:'2026-09-22T00:00:00+00:00'},{id:'2',created_at:'2026-09-15T00:00:00+00:00'},
    {id:'1',created_at:'2026-09-14T23:59:59+00:00'}];
  assert.deepEqual(refreshIds(rows, 8, now), ['3','2']);
  for (const days of [0, -1, NaN, 61]) assert.throws(() => refreshIds(rows, days, now));
});
