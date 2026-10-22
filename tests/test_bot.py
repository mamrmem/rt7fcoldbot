"""Behavior tests with fake Telegram calls. Never contact Telegram."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import App
from bot import APIError, deliver
from storage import Store, chunks, parse_leads, units


RECORDS = b'fullname: Alex Example\nphone: 07700 900001\nbusiness: Example One\n\nfullname: Blair Example\nphone: 07700 900002\n\nfullname: Casey Example\nphone: 07700 900003\n'


class FakeAPI:
    def __init__(self):
        self.files = {'file1': RECORDS}
        self.calls = []
        self.fail = None

    def download(self, file_id):
        return self.files[file_id]

    def call(self, method, payload):
        if self.fail:
            error = self.fail(method, payload)
            if error:
                raise error
        self.calls.append((method, payload))
        return {'message_id': len(self.calls), 'chat': {'id': payload.get('chat_id')}}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.s = Store(self.path)
        self.api = FakeAPI()
        self.app = App(self.s, self.api, owner_id=1)
        self.counter = 0
        self.message(1, '/start')
        self.message(1, '/setgroup', chat=-100)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def sender(self, uid):
        return {'id': uid, 'first_name': f'User {uid}', 'username': f'user{uid}'}

    def message(self, uid, text='', document=None, chat=None):
        self.counter += 1
        msg = {'message_id': self.counter, 'from': self.sender(uid),
               'chat': {'id': uid if chat is None else chat, 'type': 'private' if chat is None else 'supergroup'}}
        if text:
            msg['text'] = text
        if document:
            msg['document'] = {'file_id': document, 'file_name': 'leads.txt', 'file_size': 100}
        update = {'update_id': self.counter, 'message': msg}
        self.app.process(update)
        return update

    def click(self, uid, data, chat=None, message_id=10):
        self.counter += 1
        cb = {'id': f'cb{self.counter}', 'from': self.sender(uid), 'data': data,
              'message': {'message_id': message_id, 'chat': {'id': uid if chat is None else chat,
                          'type': 'private' if chat is None else 'supergroup'}}}
        update = {'update_id': self.counter, 'callback_query': cb}
        self.app.process(update)
        return update

    def upload(self, name='London Leads', file_id='file1'):
        self.message(1, document=file_id)
        self.message(1, name)
        return self.s.one('SELECT id FROM files WHERE name=?', (name,))['id']

    def activate(self, file_id):
        self.click(1, f'activate:{file_id}')

    def approve(self, uid):
        self.message(uid, '/start')
        self.click(1, f'approve:{uid}')

    def getlead(self, uid=2):
        self.message(uid, '/lead')
        return self.app.current(uid)

    def prepared(self):
        file_id = self.upload()
        self.activate(file_id)
        self.approve(2)
        return file_id, self.getlead()

    def ready(self, uid=2):
        lead = self.app.current(uid)
        self.click(uid, f'picked:{lead["id"]}')
        update = self.message(uid, 'Interested. Call tomorrow after 2pm.')
        self.click(uid, f'send:{lead["id"]}:{update["message"]["message_id"]}')
        self.click(uid, f'ready:{lead["id"]}')
        return self.s.one('SELECT * FROM leads WHERE id=?', (lead['id'],))

    def queued(self):
        return [(r['method'], json.loads(r['payload'])) for r in self.s.all('SELECT * FROM outbox WHERE sent=0')]

    def test_upload_name_and_activate_are_separate(self):
        file_id = self.upload()
        self.assertIsNone(self.s.get('active_file'))
        self.message(1, '/lead')
        self.assertIsNone(self.app.current(1))
        self.activate(file_id)
        self.assertEqual(self.getlead(1)['file_id'], file_id)

    def test_unapproved_users_get_no_lead_or_upload_rights(self):
        file_id = self.upload()
        self.activate(file_id)
        self.message(2, '/lead')
        self.message(2, document='file1')
        self.assertIsNone(self.app.current(2))
        self.assertEqual(len(self.s.all('SELECT * FROM files')), 1)

    def test_access_request_notifies_owner_once_and_approval_is_idempotent(self):
        self.message(2, '/start')
        self.message(2, '/start')
        notifications = [p for m,p in self.queued() if m == 'sendMessage' and p['chat_id']==1 and 'Access request' in p['text']]
        self.assertEqual(len(notifications), 1)
        self.click(3, 'approve:2')
        self.assertFalse(self.app.allowed(2))
        self.click(1, 'approve:2')
        self.click(1, 'reject:2')
        self.assertEqual(self.app.user(2)['role'], 'caller')

    def test_rejected_user_requires_explicit_owner_reapproval(self):
        self.message(2, '/start')
        self.click(1, 'reject:2')
        self.message(2, '/start')
        self.assertEqual(self.app.user(2)['role'], 'rejected')
        self.message(1, '/approve 2')
        self.assertTrue(self.app.allowed(2))

    def test_assignment_unique_and_repeated_request_resumes(self):
        _, first = self.prepared()
        self.approve(3)
        second = self.getlead(3)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.getlead(2)['id'], first['id'])
        self.assertEqual(self.s.one("SELECT COUNT(*) AS n FROM leads WHERE caller_id=2")['n'], 1)

    def test_noanswer_permanent_and_stale_buttons_cannot_change_result(self):
        _, lead = self.prepared()
        self.click(2, f'noanswer:{lead["id"]}')
        self.click(2, f'picked:{lead["id"]}')
        self.assertEqual(self.s.one('SELECT status FROM leads WHERE id=?', (lead['id'],))['status'], 'no_answer')
        self.click(2, 'next')
        self.assertNotEqual(self.app.current(2)['id'], lead['id'])
        self.click(2, 'next')
        self.assertEqual(self.s.one("SELECT COUNT(*) AS n FROM leads WHERE caller_id=2 AND status='assigned'")['n'], 1)

    def test_foreign_caller_cannot_touch_lead(self):
        _, lead = self.prepared()
        self.approve(3)
        self.click(3, f'invalid:{lead["id"]}')
        self.assertEqual(self.s.one('SELECT status FROM leads WHERE id=?', (lead['id'],))['status'], 'assigned')

    def test_invalid_and_ran_after_notes_close_permanently(self):
        _, lead = self.prepared()
        self.click(2, f'invalid:{lead["id"]}')
        self.assertIsNone(self.app.current(2))
        lead = self.getlead()
        self.click(2, f'picked:{lead["id"]}')
        note = self.message(2, 'Not interested.')
        self.click(2, f'send:{lead["id"]}:{note["message"]["message_id"]}')
        self.click(2, f'ran:{lead["id"]}')
        self.assertEqual(self.s.one('SELECT status FROM leads WHERE id=?', (lead['id'],))['status'], 'ran')

    def test_notes_are_required_and_old_review_cannot_submit_new_draft(self):
        _, lead = self.prepared()
        self.click(2, f'ready:{lead["id"]}')
        self.assertEqual(self.app.current(2)['status'], 'assigned')
        self.click(2, f'picked:{lead["id"]}')
        first = self.message(2, 'Old draft')
        second = self.message(2, 'New draft')
        self.click(2, f'send:{lead["id"]}:{first["message"]["message_id"]}')
        self.assertEqual(self.app.current(2)['status'], 'review')
        self.click(2, f'send:{lead["id"]}:{second["message"]["message_id"]}')
        self.assertEqual(self.app.current(2)['status'], 'decision')

    def test_group_handoff_contains_no_record_and_only_admin_can_claim_once(self):
        self.prepared()
        lead = self.ready()
        group_messages = [p for m,p in self.queued() if m=='sendMessage' and p['chat_id']==-100]
        handoff = [p for p in group_messages if 'Handoff #' in p['text']][0]
        self.assertIn('London Leads', handoff['text'])
        self.assertIn('Interested.', handoff['text'])
        self.assertNotIn('Alex Example', handoff['text'])
        self.assertNotIn('07700', handoff['text'])
        self.assertNotIn('fullname', handoff['text'])
        deliver(self.s, self.api, self.app.owner, limit=100)
        lead = self.s.one('SELECT * FROM leads WHERE id=?', (lead['id'],))
        self.click(2, f'take:{lead["id"]}', chat=-100, message_id=lead['group_message_id'])
        self.assertIsNone(self.s.one('SELECT claimed_by FROM leads WHERE id=?', (lead['id'],))['claimed_by'])
        self.click(1, f'take:{lead["id"]}', chat=-999, message_id=lead['group_message_id'])
        self.assertIsNone(self.s.one('SELECT claimed_by FROM leads WHERE id=?', (lead['id'],))['claimed_by'])
        self.click(1, f'take:{lead["id"]}', chat=-100, message_id=lead['group_message_id'])
        self.message(3, '/start')
        self.message(1, '/addadmin 3')
        self.click(3, f'take:{lead["id"]}', chat=-100, message_id=lead['group_message_id'])
        current = self.s.one('SELECT * FROM leads WHERE id=?', (lead['id'],))
        self.assertEqual(current['claimed_by'], 1)
        private = [p for m,p in self.queued() if m=='sendMessage' and '📝 Notes:' in p.get('text','')]
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0]['chat_id'], 1)
        self.assertIn('07700 900001', private[0]['text'])

    def test_ready_without_group_keeps_notes_and_assignment(self):
        self.prepared()
        self.s.db.execute("DELETE FROM settings WHERE key='group_id'")
        lead = self.ready()
        self.assertEqual(lead['status'], 'decision')
        self.assertIn('Interested.', lead['notes'])

    def test_switch_active_file_does_not_interrupt_assignment(self):
        old_file, old_lead = self.prepared()
        self.api.files['other'] = b'fullname: Different Person\nphone: 07700 900009'
        new_file = self.upload('Manchester Leads', 'other')
        self.assertEqual(self.s.get('active_file'), str(old_file))
        self.activate(new_file)
        self.assertEqual(self.getlead()['id'], old_lead['id'])
        self.click(2, f'noanswer:{old_lead["id"]}')
        self.assertEqual(self.getlead()['file_id'], new_file)

    def test_duplicate_uploads_do_not_reopen_closed_leads(self):
        _, lead = self.prepared()
        self.click(2, f'noanswer:{lead["id"]}')
        self.message(1, document='file1')
        self.assertIsNone(self.s.get('upload:1'))
        self.assertEqual(self.s.one('SELECT COUNT(*) AS n FROM leads')['n'], 3)
        self.assertEqual(self.s.one('SELECT status FROM leads WHERE id=?', (lead['id'],))['status'], 'no_answer')

    def test_cancel_upload_and_duplicate_filename(self):
        self.upload('London Leads')
        self.api.files['other'] = b'fullname: Another Person\nphone: 07700 900004'
        self.message(1, document='other')
        self.message(1, 'London Leads')
        self.assertIsNotNone(self.s.get('upload:1'))
        self.message(1, '/cancel')
        self.assertIsNone(self.s.get('upload:1'))
        self.assertEqual(self.s.one('SELECT COUNT(*) AS n FROM files')['n'], 1)
        self.assertEqual(self.s.one('SELECT COUNT(*) AS n FROM leads')['n'], 3)

    def test_duplicate_updates_do_not_repeat_assignments_or_messages(self):
        self.prepared()
        update = self.message(2, '/lead')
        before = self.s.one('SELECT COUNT(*) AS n FROM outbox')['n']
        self.app.process(update)
        self.assertEqual(self.s.one('SELECT COUNT(*) AS n FROM outbox')['n'], before)

    def test_restart_retains_files_open_lead_notes_cursor_and_approvals(self):
        _, lead = self.prepared()
        self.click(2, f'picked:{lead["id"]}')
        self.message(2, 'Saved across restart')
        offset = self.s.get('offset')
        self.s.close()
        self.s = Store(self.path)
        self.app = App(self.s, self.api)
        self.assertTrue(self.app.allowed(2))
        self.assertEqual(self.app.owner, 1)
        self.assertEqual(self.app.current(2)['notes'], 'Saved across restart')
        self.assertEqual(self.s.get('offset'), offset)

    def test_scores_match_outcomes(self):
        _, lead = self.prepared()
        self.click(2, f'noanswer:{lead["id"]}')
        lead = self.getlead()
        self.click(2, f'invalid:{lead["id"]}')
        self.getlead()
        self.ready()
        self.message(1, '/stats', chat=-100)
        stats = [p['text'] for m,p in self.queued() if m=='sendMessage' and 'Team Progress' in p['text']][-1]
        self.assertIn('@user2 — ✅ 1 · ❌ 1 · 📵 1', stats)
        self.assertIn('Available: 0 · In progress: 0', stats)

    def test_nonadmin_group_stats_never_exposes_data(self):
        self.prepared()
        before = len(self.queued())
        self.message(2, '/stats', chat=-100)
        self.assertEqual(len(self.queued()), before)

    def test_note_and_lead_lengths_respect_telegram_limits(self):
        self.prepared()
        lead = self.app.current(2)
        self.click(2, f'picked:{lead["id"]}')
        self.message(2, '😀' * 1300)
        self.assertEqual(self.app.current(2)['status'], 'notes')
        for _, payload in self.queued():
            if 'text' in payload:
                self.assertLessEqual(units(payload['text']), 4096)

    def test_ownerless_setup_has_no_first_user_takeover(self):
        other = Store(':memory:')
        app = App(other, self.api)
        app.process({'update_id': 1, 'message': {'message_id': 1, 'from': self.sender(999),
                     'chat': {'id': 999, 'type': 'private'}, 'text': '/start'}})
        self.assertFalse(app.admin(999))
        self.assertFalse(app.allowed(999))
        self.assertIsNone(other.get('owner_id'))
        other.close()

    def test_transient_delivery_failure_is_retried(self):
        with self.s.transaction():
            self.s.say(-100, 'Retry me', kind='handoff', reference=None)
        self.api.fail = lambda method, payload: APIError(503) if payload.get('text')=='Retry me' else None
        deliver(self.s, self.api, 1, 100)
        row = self.s.one("SELECT * FROM outbox WHERE sent=0 AND attempts>0")
        self.assertIsNotNone(row)
        self.api.fail = None
        self.s.db.execute('UPDATE outbox SET next_try=0')
        deliver(self.s, self.api, 1, 100)
        self.assertEqual(self.s.one('SELECT sent FROM outbox WHERE id=?', (row['id'],))['sent'], 1)

    def test_failed_admin_dm_is_not_sent_to_group_and_can_be_retried(self):
        self.prepared()
        lead = self.ready()
        deliver(self.s, self.api, 1, 100)
        lead = self.s.one('SELECT * FROM leads WHERE id=?', (lead['id'],))
        self.message(3, '/start')
        self.message(1, '/addadmin 3')
        deliver(self.s, self.api, 1, 100)
        self.click(3, f'take:{lead["id"]}', chat=-100, message_id=lead['group_message_id'])
        self.api.fail = lambda method,p: APIError(403) if method=='sendMessage' and p.get('chat_id')==3 else None
        deliver(self.s, self.api, 1, 100)
        failed = self.s.one("SELECT * FROM outbox WHERE sent=-1 AND kind='claim_details'")
        self.assertIsNotNone(failed)
        self.assertEqual(self.s.one('SELECT claimed_by FROM leads WHERE id=?', (lead['id'],))['claimed_by'], 3)
        self.assertFalse(any(p.get('chat_id')==-100 and '07700' in p.get('text','') for _,p in self.api.calls))
        self.api.fail = None
        self.message(1, '/retry')
        deliver(self.s, self.api, 1, 100)
        self.assertEqual(self.s.one('SELECT sent FROM outbox WHERE id=?', (failed['id'],))['sent'], 1)

    def test_concurrent_assignments_on_separate_connections_do_not_duplicate(self):
        file_id = self.upload()
        self.activate(file_id)
        self.approve(2)
        self.approve(3)
        errors = []
        barrier = threading.Barrier(2)
        def allocate(uid):
            store = Store(self.path)
            try:
                app = App(store, self.api, owner_id=1)
                barrier.wait(timeout=5)
                with store.transaction():
                    app.lead(uid)
            except Exception as error:
                errors.append(error)
            finally:
                store.close()
        threads = [threading.Thread(target=allocate, args=(uid,)) for uid in (2,3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertNotEqual(self.app.current(2)['id'], self.app.current(3)['id'])

    def test_failed_download_rolls_back_state_and_cursor_for_retry(self):
        before = self.s.get('offset')
        with patch.object(self.api, 'download', side_effect=APIError(503)):
            with self.assertRaises(APIError):
                self.message(1, document='file1')
        self.assertEqual(self.s.get('offset'), before)
        self.assertEqual(self.s.one('SELECT COUNT(*) AS n FROM files')['n'], 0)
        self.assertIsNone(self.s.get('upload:1'))

    def test_rate_limit_pauses_remaining_delivery_until_retry_time(self):
        self.api.fail = lambda method,payload: APIError(429, retry_after=60)
        deliver(self.s, self.api, 1, 100)
        self.assertIsNotNone(self.s.get('telegram_paused_until'))
        self.api.fail = None
        calls = len(self.api.calls)
        deliver(self.s, self.api, 1, 100)
        self.assertEqual(len(self.api.calls), calls)
        self.s.set('telegram_paused_until', 0)
        self.s.db.execute('UPDATE outbox SET next_try=0')
        deliver(self.s, self.api, 1, 100)
        self.assertGreater(len(self.api.calls), calls)

    def test_group_only_admin_must_start_privately_before_claiming(self):
        self.prepared()
        lead = self.ready()
        self.app.config_admins.add(3)
        self.message(3, '/stats', chat=-100)
        self.click(3, f'take:{lead["id"]}', chat=-100)
        self.assertIsNone(self.s.one('SELECT claimed_by FROM leads WHERE id=?', (lead['id'],))['claimed_by'])
        self.message(3, '/start')
        self.click(3, f'take:{lead["id"]}', chat=-100)
        self.assertEqual(self.s.one('SELECT claimed_by FROM leads WHERE id=?', (lead['id'],))['claimed_by'], 3)


class ParserTests(unittest.TestCase):
    def test_multiple_records_multiline_and_case_insensitive_marker(self):
        records = parse_leads(b'FULLNAME: Alex\nphone: 123\nnotes: one\n two\n\nFull Name = Blair\nphone: 456')
        self.assertEqual(len(records), 2)
        self.assertIn('notes: one\n two', records[0])

    def test_bom_utf16_and_legacy_encoding(self):
        for raw in ['fullname: Zoë\nphone: 123'.encode('utf-16'), b'\xef\xbb\xbffullname: Alex', b'fullname: Jos\xe9']:
            self.assertEqual(len(parse_leads(raw)), 1)

    def test_rejects_preamble_missing_marker_and_binary(self):
        for raw in [b'header\nfullname: Alex', b'no names here', b'fullname: A\x00B', b'']:
            with self.assertRaises(ValueError):
                parse_leads(raw)

    def test_marker_only_at_line_start(self):
        records = parse_leads(b'fullname: Alex\nnotes: the fullname: field stays here\nfullname: Blair')
        self.assertEqual(len(records), 2)

    def test_chunking_preserves_unicode_without_exceeding_limits(self):
        text = '😀🚀x\n' * 2500
        parts = list(chunks(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(units(p)<=3500 for p in parts))


if __name__ == '__main__':
    unittest.main()
