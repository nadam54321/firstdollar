import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

spec = importlib.util.spec_from_file_location('archive', Path(__file__).resolve().parents[1] / 'scripts/archive.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = archive.connect(Path(self.tmp.name) / 'db.sqlite3')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def payload(self, text='hello', metrics=None):
        return {'success': True, 'command': 'search', 'data': [{
            'id': '2102254361401323591', 'text': text, 'author': {'username': 'Someone'},
            'created_at': 'Tue Sep 22 04:31:12 +0000 2026',
            'is_reply': True, 'in_reply_to_id': '999', 'metrics': metrics or {}}]}

    def test_idempotence_and_later_metrics_preserve_text_history(self):
        p = self.payload(metrics={'likes': 1})
        archive.ingest(self.db, p, observed_at='2026-09-22T05:00:00Z')
        self.assertTrue(archive.ingest(self.db, p)['duplicate_payload'])
        archive.ingest(self.db, self.payload('edited', {'likes': 2}), observed_at='2026-09-23T05:00:00Z')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM post_versions').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT author,text FROM posts').fetchone()[:], ('someone','edited'))

    def test_bad_batch_cannot_partially_import(self):
        p = self.payload()
        p['data'].append({'id': None, 'text': 'broken'})
        with self.assertRaises(ValueError): archive.ingest(self.db, p)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 0)

    def test_unknown_metrics_and_missing_parent_remain_unknown(self):
        archive.ingest(self.db, self.payload())
        self.assertEqual(self.db.execute('SELECT metrics_json FROM observations').fetchone()[0], '{}')
        self.assertEqual(self.db.execute('SELECT parent_id FROM posts').fetchone()[0], '999')
        self.assertFalse(self.db.execute('SELECT * FROM posts WHERE id="999"').fetchall())

    def test_replies_need_approval_before_posting(self):
        with self.assertRaises(ValueError): archive.draft_reply(self.db, '404', 'hello')
        archive.prospects_add(self.db, [{'post_id': '5', 'author': 'ann', 'text': 'need an agent', 'url': 'https://x.com/ann/status/5'}])
        rid = archive.draft_reply(self.db, '5', '  hi ann  ', untick=['@BigBrand'])
        with self.assertRaises(ValueError): archive.move_reply(self.db, rid, 'posted')
        archive.move_reply(self.db, rid, 'approved')
        self.assertEqual(archive.queue(self.db, 'approved')[0]['untick'], '["bigbrand"]')
        archive.move_reply(self.db, rid, 'posted', reply_id='55')
        self.assertEqual(self.db.execute('SELECT status,reply_id FROM prospects').fetchone()[:], ('replied', '55'))
        with self.assertRaises(ValueError): archive.move_reply(self.db, rid, 'approved')
        self.assertEqual(archive.limits(self.db)['posted_today'], 1)

    def test_limits_count_today_and_enforce_spacing(self):
        archive.prospects_add(self.db, [{'post_id': str(i), 'author': 'a', 'text': 'need'} for i in (1, 2)])
        for i in (1, 2):
            archive.move_reply(self.db, archive.draft_reply(self.db, str(i), 'hi'), 'approved')
        archive.move_reply(self.db, 1, 'posted')
        self.db.execute("UPDATE replies SET posted_at='2026-09-22T10:00:00+00:00' WHERE id=1"); self.db.commit()
        archive.move_reply(self.db, 2, 'posted')
        state = archive.limits(self.db, current=archive.parse_time(self.db.execute('SELECT posted_at FROM replies WHERE id=2').fetchone()[0]))
        self.assertEqual(state['posted_today'], 1)
        self.assertGreater(state['wait_seconds'], 0)

    def post(self, id, created, text='hello', parent=None, author='me', followers=None, observed=()):
        for i, (at, metrics) in enumerate(observed or [(created, {})]):
            archive.ingest(self.db, {'success': True, 'command': f'test{i}', 'data': [{
                'id': id, 'text': text, 'author': {'username': author}, 'created_at': created,
                'is_reply': parent is not None, 'in_reply_to_id': parent, 'metrics': metrics,
                'source_tweet': {'author': {'followers': followers}}}]}, observed_at=at)

    def test_theme_is_single_valued_and_from_the_vocabulary(self):
        self.post('1', '2026-09-20T00:00:00+00:00')
        archive.THEMES = ('AI choice', 'saved work')
        self.addCleanup(setattr, archive, 'THEMES', ())
        with self.assertRaises(ValueError): archive.tag(self.db, '1', 'theme', 'made up', 'test')
        archive.tag(self.db, '1', 'theme', 'AI choice', 'test')
        archive.tag(self.db, '1', 'theme', 'saved work', 'test')
        self.assertEqual([r[0] for r in self.db.execute("SELECT value FROM annotations WHERE category='theme'")], ['saved work'])

    def test_cli_tags_and_deleted_posts_leave_the_report(self):
        self.post('1', '2026-09-20T00:00:00+00:00', observed=[('2026-09-21T00:00:00+00:00', {'views': 5})])
        self.db.commit()
        script = str(Path(__file__).resolve().parents[1] / 'scripts/archive.py')
        path = str(Path(self.tmp.name) / 'db.sqlite3')
        for args in (['theme', 'saved work'], ['status', 'deleted']):
            subprocess.run([sys.executable, script, '--db', path, 'tag', '1', *args, '--provenance', 'test'], check=True)
        self.assertEqual(archive.report(self.db, 30, authors=('me',), current=archive.parse_time('2026-09-22T00:00:00+00:00'))['posts'], [])
        with self.assertRaises(ValueError): archive.tag(self.db, '1', 'status', 'hidden', 'test')

    def test_report_compares_posts_at_the_same_age(self):
        self.post('9', '2026-09-10T00:00:00+00:00', 'parent', author='minchoi', followers=388000)
        self.post('1', '2026-09-20T00:00:00+00:00', '@minchoi reply with $39', parent='9', observed=[
            ('2026-09-20T06:00:00+00:00', {'views': 10, 'likes': 1}),
            ('2026-09-21T01:00:00+00:00', {'views': 100, 'likes': 2}),
            ('2026-09-21T13:00:00+00:00', {'views': 120, 'likes': 2}),
            ('2026-09-27T23:00:00+00:00', {'views': 300, 'likes': 3, 'bookmarks': 1})])
        self.post('2', '2026-09-22T00:00:00+00:00', 'Grok Bot, Muse and Instinct', observed=[
            ('2026-09-22T02:00:00+00:00', {'views': 50})])
        archive.tag(self.db, '1', 'theme', 'latest model', 'test')
        archive.CONFIG['competitors'] = ['Grok Bot', 'Muse']
        self.addCleanup(archive.CONFIG.__setitem__, 'competitors', [])
        result = archive.report(self.db, 30, authors=('me',), current=archive.parse_time('2026-09-28T00:00:00+00:00'))
        reply = next(p for p in result['posts'] if p['id'] == '1')
        self.assertEqual((reply['day1']['views'], reply['day7']['views'], reply['latest']['views']), (100, 300, 300))
        self.assertEqual((reply['kind'], reply['audience'], reply['theme'], reply['price']), ('reply', '100k+', 'latest model', True))
        self.assertAlmostEqual(reply['engagement'], 4 / 300)
        original = next(p for p in result['posts'] if p['id'] == '2')
        self.assertIsNone(original['day1'])
        self.assertTrue(original['competitors'])
        self.assertNotIn('9', [p['id'] for p in result['posts']])
        self.assertIn('BY THEME', archive.render(result))

    def test_analytics_imports_posts_or_days_and_nothing_on_error(self):
        folder = Path(self.tmp.name)
        posts = folder / 'posts.csv'
        posts.write_text('Post id,Post text,Post Link,Impressions,URL Clicks,Profile visits\n'
                         '2102604382932222011,hello,https://x.com/a/status/2102604382932222011,"1,204",7,3\n'
                         ',no id,https://x.com/a/status/42,10,1,0\n')
        result = archive.import_analytics(self.db, posts, '2026-09-24T00:00:00+00:00')
        self.assertEqual((result['kind'], result['rows']), ('posts', 2))
        metrics = dict(self.db.execute('SELECT post_id,metrics_json FROM analytics_exports').fetchall())
        self.assertIn('"url_clicks": 7', metrics['2102604382932222011'])
        self.assertIn('42', metrics)
        daily = folder / 'daily.csv'
        daily.write_text('Date,Impressions,Profile visits\n"Tue, Sep 23, 2026",5000,12\n')
        archive.import_analytics(self.db, daily, account='@Me')
        self.assertEqual(self.db.execute("SELECT value,source FROM daily_signals WHERE key='x_profile_visits'").fetchone()[:],
                         (12, 'x analytics export @me'))
        broken = folder / 'broken.csv'
        broken.write_text('Post text,Impressions\nhello,5\n')
        with self.assertRaises(ValueError): archive.import_analytics(self.db, broken)
        bad_row = folder / 'bad.csv'
        bad_row.write_text('Post id,Impressions\n123,5\nnot-an-id,6\n')
        with self.assertRaises(ValueError): archive.import_analytics(self.db, bad_row, 'later')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM analytics_exports WHERE exported_at='later'").fetchone()[0], 0)

    def test_prospects_keep_triage_and_flag_earlier_contact(self):
        row = {'post_id': '77', 'url': 'https://x.com/a/status/77', 'author': 'Ann', 'text': 'I want my own AI', 'query': 'q'}
        self.assertEqual(archive.prospects_add(self.db, [row])['new'], 1)
        archive.prospect_mark(self.db, '77', 'skipped', 'price-sensitive')
        self.assertEqual(archive.prospects_add(self.db, [row])['new'], 0)
        self.assertEqual(self.db.execute('SELECT status,reason FROM prospects').fetchone()[:], ('skipped', 'price-sensitive'))
        with self.assertRaises(ValueError): archive.prospect_mark(self.db, '77', 'replied', reply_id='not-an-id')
        with self.assertRaises(ValueError): archive.prospects_add(self.db, [{'post_id': 'x', 'text': 'hi'}])
        archive.prospect_mark(self.db, '77', 'replied', reply_id='88')
        archive.prospects_add(self.db, [dict(row, post_id='78')])
        self.assertEqual(archive.prospects_list(self.db)[0]['contacted'], 1)

    def test_funnel_counts_later_stages_as_reached(self):
        archive.prospects_add(self.db, [{'post_id': '1', 'author': 'a', 'text': 'need'}, {'post_id': '2', 'author': 'b', 'text': 'need'}])
        with self.assertRaises(ValueError): archive.prospect_stage(self.db, '1', 'answered')
        archive.prospect_mark(self.db, '1', 'replied', reply_id='11')
        archive.prospect_mark(self.db, '2', 'replied', reply_id='22')
        archive.prospect_stage(self.db, '1', 'offered', 'has price')
        with self.assertRaises(ValueError): archive.prospect_stage(self.db, '2', 'paid')
        f = archive.funnel(self.db)
        self.assertEqual((f['replied'], f['answered'], f['offered'], f['ordered']), (2, 1, 1, 0))

    def test_voice_samples_own_writing_without_links_or_mentions(self):
        self.post('1', '2026-09-20T00:00:00+00:00', '@someone honestly the cap is the problem here, not you. try running it somewhere that stays on https://t.co/x')
        self.post('2', '2026-09-21T00:00:00+00:00', 'short one')
        self.post('3', '2026-09-22T00:00:00+00:00', 'not mine but long enough to count as a sample of writing', author='other')
        samples = archive.voice(self.db, authors=('me',))
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]['text'], 'honestly the cap is the problem here, not you. try running it somewhere that stays on')

    def test_answers_move_first_reply_to_answered(self):
        archive.prospects_add(self.db, [{'post_id': '7', 'author': 'Ann', 'text': 'need'}])
        archive.prospect_mark(self.db, '7', 'replied', reply_id='70')
        seen = archive.answers_seen(self.db, [{'author': '@ann', 'text': 'thanks, how much is it?'}, {'author': 'bob', 'text': 'hi'}])
        self.assertEqual([s['lead'] is not None for s in seen], [True, False])
        self.assertEqual(archive.funnel(self.db)['answered'], 1)

    def test_signal_needs_a_real_day(self):
        archive.signal(self.db, '2026-09-23', 'Purchases', 1, 'founder')
        self.assertEqual(self.db.execute('SELECT key,value FROM daily_signals').fetchone()[:], ('purchases', 1.0))
        with self.assertRaises(ValueError): archive.signal(self.db, '23-09-2026', 'purchases', 1, 'founder')

    def test_prune_keeps_recent_backups_and_one_per_older_day(self):
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir()
        names = ['content-20260923T010000000000Z.sqlite3', 'content-20260922T230000000000Z.sqlite3',
                 'content-20260915T230000000000Z.sqlite3', 'content-20260915T010000000000Z.sqlite3',
                 'content-20260914T010000000000Z.sqlite3', 'notes.txt']
        for name in names: (folder / name).write_text('x')
        removed = archive.prune_backups(folder, archive.parse_time('2026-09-23T12:00:00+00:00'))
        self.assertEqual([Path(p).name for p in removed], ['content-20260915T010000000000Z.sqlite3'])
        self.assertEqual(len(list(folder.iterdir())), 5)

    def test_integrity_check_fails_on_corruption_and_broken_references(self):
        archive.check(self.db)
        corrupt = Mock()
        corrupt.execute.return_value = [('corrupt page',)]
        with self.assertRaises(ValueError): archive.check(corrupt)
        self.db.commit()
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute('INSERT INTO relationships VALUES(?,?,?)', ('missing','quote','999'))
        with self.assertRaises(ValueError): archive.check(self.db)


if __name__ == '__main__': unittest.main()
