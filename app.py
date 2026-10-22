"""Telegram command and button workflows; transport-independent for tests."""
from __future__ import annotations

import sqlite3

from storage import OPEN_STATES, fingerprint, now, parse_leads, units


def button(text, data):
    return {'text': text, 'callback_data': data}


def label(user):
    if user and user['username']:
        return '@' + user['username']
    return (user['name'] + ' (' + str(user['id']) + ')') if user else 'Unknown user'


class App:
    def __init__(self, store, api, owner_id=0, admin_ids=(), group_id=0):
        self.s, self.api = store, api
        with store.transaction():
            existing_owner = int(store.get('owner_id', '0'))
            if owner_id and existing_owner and owner_id != existing_owner:
                raise ValueError('OWNER_USER_ID differs from the stored owner. Use the original owner ID.')
            self.owner = owner_id or existing_owner
            if self.owner:
                store.set('owner_id', self.owner)
            self.config_admins = set(admin_ids) | ({self.owner} if self.owner else set())
            if group_id and not store.get('group_id'):
                store.set('group_id', group_id)

    def user(self, user_id):
        return self.s.one('SELECT * FROM users WHERE id=?', (user_id,))

    def admin(self, user_id):
        user = self.user(user_id)
        return user_id in self.config_admins or bool(user and user['role'] == 'admin')

    def allowed(self, user_id):
        user = self.user(user_id)
        return self.admin(user_id) or bool(user and user['role'] == 'caller')

    def remember(self, sender, private=False):
        name = ' '.join(str(sender.get(k, '')) for k in ('first_name', 'last_name')).strip() or 'Telegram user'
        self.s.db.execute('''INSERT INTO users(id,username,name,private_started,updated)
          VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          username=excluded.username,name=excluded.name,updated=excluded.updated,
          private_started=MAX(users.private_started,excluded.private_started)''',
          (sender['id'], sender.get('username'), name[:200], int(private), now()))
        if sender['id'] in self.config_admins:
            self.s.db.execute("UPDATE users SET role='admin' WHERE id=?", (sender['id'],))

    def process(self, update):
        """State changes, audit, outgoing messages and update cursor commit together."""
        update_id = int(update['update_id'])
        if update_id < int(self.s.get('offset', '0')):
            return
        with self.s.transaction():
            if 'callback_query' in update:
                self.callback(update['callback_query'])
            elif 'message' in update:
                self.message(update['message'])
            self.s.set('offset', update_id + 1)

    def answer(self, cb, text='', alert=False):
        self.s.queue('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': text[:180], 'show_alert': alert})

    def clear_buttons(self, cb):
        msg = cb.get('message', {})
        if msg.get('message_id'):
            self.s.queue('editMessageReplyMarkup', {'chat_id': msg['chat']['id'],
                'message_id': msg['message_id'], 'reply_markup': {'inline_keyboard': []}})

    def event(self, actor, lead, action, detail=''):
        self.s.audit(actor, lead, action, detail)

    def request_access(self, uid):
        user = self.user(uid)
        if self.allowed(uid):
            self.s.say(uid, '✅ You have access. Send /lead to begin.\n' +
                ('Admin: upload a .txt file, name it, then use /work.\nUse /help for all commands.' if self.admin(uid) else 'Use /mystats to view your progress.'))
            return
        if user['role'] in ('rejected', 'revoked'):
            self.s.say(uid, 'Your access is not approved. Contact the bot owner.')
            return
        if not user['requested']:
            self.s.db.execute('UPDATE users SET requested=1 WHERE id=?', (uid,))
            self.event(uid, None, 'access_requested')
            self.s.say(self.owner, f'🔔 Access request\n{label(user)}\nTelegram ID: {uid}',
                [[button('Approve', f'approve:{uid}'), button('Reject', f'reject:{uid}')]])
        self.s.say(uid, 'Your access request has been sent. Please wait for approval.')

    def current(self, uid):
        return self.s.one("SELECT l.*,f.name AS file_name FROM leads l JOIN files f ON f.id=l.file_id "
            "WHERE l.caller_id=? AND l.status IN ('assigned','notes','review','decision')", (uid,))

    def lead(self, uid):
        current = self.current(uid)
        if current:
            self.show_lead(uid, current)
            return
        file_id = int(self.s.get('active_file', '0'))
        lead = self.s.one("SELECT l.*,f.name AS file_name FROM leads l JOIN files f ON f.id=l.file_id "
            "WHERE l.file_id=? AND f.name IS NOT NULL AND l.status='available' ORDER BY l.id LIMIT 1", (file_id,))
        if not lead:
            name = self.s.one('SELECT name FROM files WHERE id=?', (file_id,))
            self.s.say(uid, f'No available leads left in {name["name"]}. Ask an admin to choose another file with /work.'
                       if name else 'No active file yet. An admin must upload a file and select it with /work.')
            return
        self.s.db.execute("UPDATE leads SET status='assigned',caller_id=?,assigned_at=? WHERE id=? AND status='available'",
                          (uid, now(), lead['id']))
        self.event(uid, lead['id'], 'assigned')
        self.show_lead(uid, self.current(uid))

    def next_button(self):
        return [[button('Next Lead', 'next')]]

    def show_lead(self, uid, lead):
        lid = lead['id']
        if lead['status'] == 'assigned':
            keys = [[button('📞 Picked', f'picked:{lid}')],
                    [button('📵 No Answer', f'noanswer:{lid}'), button('❌ Ran / Invalid', f'invalid:{lid}')]]
            self.s.say(uid, f'📁 {lead["file_name"]}\nLead #{lid}\n\n{lead["body"]}', keys)
        elif lead['status'] == 'notes':
            self.s.say(uid, f'📝 Lead #{lid}: write your call notes as one message.\nUse /showlead to view the details again.')
        elif lead['status'] == 'review':
            self.preview(uid, lead)
        else:
            self.decision(uid, lead)

    def preview(self, uid, lead):
        self.s.say(uid, f'📝 Review notes for lead #{lead["id"]}:\n\n{lead["notes"]}\n\n'
                   'These notes will appear in the group if you choose Ready.',
            [[button('Send', f'send:{lead["id"]}:{lead["note_message_id"]}'),
              button('Rewrite notes', f'editnotes:{lead["id"]}')]])

    def decision(self, uid, lead):
        self.s.say(uid, f'Lead #{lead["id"]}: what is the outcome?',
            [[button('✅ Ready', f'ready:{lead["id"]}'), button('❌ Ran', f'ran:{lead["id"]}')],
             [button('Edit notes', f'editnotes:{lead["id"]}')]])

    def work(self, uid, page=0):
        files = self.s.all('''SELECT f.id,f.name,COUNT(CASE WHEN l.status='available' THEN 1 END) AS available
          FROM files f LEFT JOIN leads l ON l.file_id=f.id WHERE f.name IS NOT NULL
          GROUP BY f.id ORDER BY f.id''')
        if not files:
            self.s.say(uid, 'No saved files yet. Upload a .txt file here, then send its name.')
            return
        pages = (len(files) + 7) // 8
        page = max(0, min(page, pages - 1))
        active = int(self.s.get('active_file', '0'))
        keys = [[button(('✅ ' if f['id'] == active else '') + f'{f["name"]} · {f["available"]} available',
                        f'activate:{f["id"]}')] for f in files[page*8:page*8+8]]
        nav = []
        if page:
            nav.append(button('← Previous', f'files:{page-1}'))
        if page + 1 < pages:
            nav.append(button('More →', f'files:{page+1}'))
        if nav:
            keys.append(nav)
        self.s.say(uid, f'Choose the active file for everyone (page {page+1}/{pages}):', keys)

    def upload(self, uid, document):
        if self.s.get(f'upload:{uid}'):
            self.s.say(uid, 'Name your previous upload first, or use /cancel to discard it.')
            return
        active = self.current(uid)
        if active and active['status'] in ('notes', 'review', 'decision'):
            self.s.say(uid, 'Finish the current lead outcome before uploading a file. Send /lead to resume.')
            return
        original = document.get('file_name', '')
        if not original.lower().endswith('.txt'):
            self.s.say(uid, 'Please upload a .txt document. Each lead must begin with fullname: on its own line.')
            return
        if document.get('file_size', 0) > 19_000_000:
            self.s.say(uid, 'Please split this into .txt files smaller than 19 MB.')
            return
        try:
            records = parse_leads(self.api.download(document['file_id']))
        except ValueError as error:
            self.s.say(uid, str(error))
            return
        cursor = self.s.db.execute('INSERT INTO files(original_name,owner_id,created) VALUES(?,?,?)',
                                   (original[:255], uid, now()))
        file_id, skipped = cursor.lastrowid, 0
        for record in records:
            cursor = self.s.db.execute('INSERT OR IGNORE INTO leads(file_id,body,fingerprint) VALUES(?,?,?)',
                                      (file_id, record, fingerprint(record)))
            skipped += int(cursor.rowcount == 0)
        saved = len(records) - skipped
        if not saved:
            self.s.db.execute('DELETE FROM files WHERE id=?', (file_id,))
            self.s.say(uid, 'No new leads to save: all records already exist in the bot. Closed leads stay closed.')
            return
        self.s.db.execute('UPDATE files SET skipped=? WHERE id=?', (skipped, file_id))
        self.s.set(f'upload:{uid}', file_id)
        self.event(uid, None, 'file_uploaded', f'file={file_id};new={saved};duplicates={skipped}')
        extra = f'\nSkipped {skipped} duplicate records already saved.' if skipped else ''
        self.s.say(uid, f'📎 Found {saved} new leads.{extra}\nWhat name should I save this file under?\n'
                   'Send a name of 1–80 characters. /cancel discards this unnamed upload.')

    def name_upload(self, uid, text):
        name = text.strip()
        if not name or '\n' in name or units(name) > 80 or name.startswith('/') or any(ord(c) < 32 for c in name):
            self.s.say(uid, 'Use a single-line name of 1–80 characters, without a leading /.')
            return
        file_id = int(self.s.get(f'upload:{uid}'))
        try:
            self.s.db.execute('UPDATE files SET name=? WHERE id=? AND owner_id=?', (name, file_id, uid))
        except sqlite3.IntegrityError:
            self.s.say(uid, 'That file name already exists. Please choose a different name.')
            return
        self.s.db.execute('DELETE FROM settings WHERE key=?', (f'upload:{uid}',))
        count = self.s.one('SELECT COUNT(*) AS n FROM leads WHERE file_id=?', (file_id,))['n']
        self.event(uid, None, 'file_named', f'file={file_id};name={name}')
        self.s.say(uid, f'✅ Saved {name} — {count} leads.\nUpload another file, or use /work to select the active file.')

    def save_notes(self, uid, lead, msg):
        notes = msg['text'].strip()
        if not notes or units(notes) > 2500:
            self.s.say(uid, 'Write non-empty notes of at most 2,500 characters as one message.')
            return
        self.s.db.execute("UPDATE leads SET status='review',notes=?,note_message_id=? WHERE id=?",
                          (notes, msg['message_id'], lead['id']))
        self.event(uid, lead['id'], 'notes_drafted')
        self.preview(uid, self.current(uid))

    def message(self, msg):
        sender = msg.get('from')
        if not sender or sender.get('is_bot'):
            return
        private = msg['chat']['type'] == 'private'
        # Anonymous group admins cannot identify themselves securely; use a personal account.
        if sender['id'] == 1087968824:
            return
        uid, chat = sender['id'], msg['chat']['id']
        self.remember(sender, private)
        text = msg.get('text', '').strip()
        first = text.split(maxsplit=1)[0] if text else ''
        cmd = first.split('@')[0].lower() if first.startswith('/') else ''
        argument = text[len(first):].strip() if first else ''
        if cmd in ('/id', '/whoami'):
            if private:
                self.s.say(uid, f'Your Telegram user ID: {uid}')
            return
        if not self.owner:
            if private:
                self.s.say(uid, f'Your Telegram user ID: {uid}\nSetup is not complete. '
                           'The operator must set OWNER_USER_ID in the hosting environment and restart the bot.')
            return
        if not private:
            migration = msg.get('migrate_to_chat_id')
            if migration and chat == int(self.s.get('group_id', '0')):
                self.s.set('group_id', migration)
                self.s.db.execute('UPDATE leads SET group_chat_id=? WHERE group_chat_id=?', (migration, chat))
                self.event(uid, None, 'group_migrated', f'{chat}:{migration}')
                return
            if cmd == '/setgroup' and uid == self.owner and msg['chat']['type'] in ('group', 'supergroup'):
                self.s.set('group_id', chat)
                self.s.set('group_thread', msg.get('message_thread_id', 0))
                self.event(uid, None, 'group_connected', str(chat))
                self.s.say(chat, '✅ This group/topic is connected. Ready leads will appear here with notes and an admin-only Take Further button.',
                           thread=msg.get('message_thread_id'))
            elif cmd == '/stats' and self.admin(uid) and chat == int(self.s.get('group_id', '0')):
                self.stats(chat, uid, group=True)
            return
        if cmd == '/start':
            self.request_access(uid)
        elif cmd == '/help':
            self.help(uid)
        elif not self.allowed(uid):
            self.s.say(uid, 'Access is required. Send /start to request approval.')
        elif cmd in ('/lead', '/next'):
            if self.s.get(f'upload:{uid}'):
                self.s.say(uid, 'Name your uploaded file first, or send /cancel.')
            else:
                self.lead(uid)
        elif cmd == '/showlead':
            lead = self.current(uid)
            self.s.say(uid, f'📁 {lead["file_name"]}\nLead #{lead["id"]}\n\n{lead["body"]}' if lead else 'No current lead. Send /lead.')
        elif cmd in ('/mystats', '/stats'):
            self.stats(uid, uid, personal=cmd == '/mystats' or not self.admin(uid))
        elif cmd in ('/work', '/files') and self.admin(uid):
            self.work(uid)
        elif cmd == '/cancel' and self.admin(uid):
            pending = int(self.s.get(f'upload:{uid}', '0'))
            if pending:
                self.s.db.execute('DELETE FROM leads WHERE file_id=?', (pending,))
                self.s.db.execute('DELETE FROM files WHERE id=? AND name IS NULL', (pending,))
                self.s.db.execute('DELETE FROM settings WHERE key=?', (f'upload:{uid}',))
                self.event(uid, None, 'upload_discarded', str(pending))
                self.s.say(uid, 'Unnamed upload discarded. You can upload another file.')
            else:
                self.s.say(uid, 'No unnamed upload to discard. Your current lead stays assigned; send /lead to resume.')
        elif cmd == '/pending' and self.admin(uid):
            pending = self.s.all("SELECT * FROM users WHERE role='pending' AND requested=1 ORDER BY id")
            if not pending:
                self.s.say(uid, 'No pending access requests.')
            for user in pending:
                self.s.say(uid, f'{label(user)}\nID: {user["id"]}',
                    [[button('Approve', f'approve:{user["id"]}'), button('Reject', f'reject:{user["id"]}')]])
        elif cmd == '/team' and self.admin(uid):
            rows = self.s.all("SELECT * FROM users WHERE role IN ('admin','caller') ORDER BY role,id")
            self.s.say(uid, 'Team\n\n' + '\n'.join(f'{label(u)} · {u["role"]} · ID {u["id"]}' for u in rows))
        elif cmd in ('/addadmin', '/removeadmin', '/revoke', '/approve') and uid == self.owner:
            self.manage_user(uid, cmd, argument)
        elif cmd == '/history' and self.admin(uid):
            self.history(uid, argument)
        elif cmd == '/status' and self.admin(uid):
            group = self.s.get('group_id', 'not connected')
            active = self.s.one('SELECT name FROM files WHERE id=?', (int(self.s.get('active_file', '0')),))
            errors = self.s.one('SELECT COUNT(*) AS n FROM outbox WHERE sent=-1')['n']
            self.s.say(uid, f'Group ID: {group}\nActive file: {active["name"] if active else "none"}\n'
                       f'Failed notifications: {errors}\nUse /retry to retry failed notifications after fixing permissions.')
        elif cmd == '/retry' and self.admin(uid):
            self.s.db.execute('UPDATE outbox SET sent=0,attempts=0,next_try=0 WHERE sent=-1')
            self.s.say(uid, 'Failed notifications have been queued for retry. If an admin cannot receive DMs, ask them to /start the bot first.')
        elif cmd:
            self.s.say(uid, 'Unknown or unavailable command. Use /help.')
        elif 'document' in msg:
            if self.admin(uid):
                self.upload(uid, msg['document'])
            else:
                self.s.say(uid, 'Only admins can upload lead files. Use /lead to get your next lead.')
        elif self.admin(uid) and self.s.get(f'upload:{uid}') and text:
            self.name_upload(uid, text)
        elif text and self.current(uid) and self.current(uid)['status'] in ('notes', 'review'):
            self.save_notes(uid, self.current(uid), msg)
        else:
            self.s.say(uid, 'Send /lead to get or resume your lead. Use /help for commands.')

    def callback(self, cb):
        sender, msg = cb.get('from'), cb.get('message', {})
        if not sender or not msg.get('chat'):
            return
        uid = sender['id']
        private = msg['chat']['type'] == 'private' and msg['chat']['id'] == uid
        self.remember(sender)
        data = cb.get('data', '').split(':')
        action = data[0]
        if not self.owner:
            self.answer(cb, 'The owner must finish setup first.', True)
            return
        if action == 'take':
            try:
                lid = int(data[1])
            except (IndexError, ValueError):
                self.answer(cb, 'Invalid button.', True)
                return
            self.take(cb, uid, lid)
            return
        if not private:
            self.answer(cb, 'Use this button in your private chat with the bot.', True)
            return
        if action in ('approve', 'reject', 'activate', 'files'):
            if not self.admin(uid):
                self.answer(cb, 'Only admins can do this.', True)
                return
            try:
                value = int(data[1])
            except (IndexError, ValueError):
                self.answer(cb, 'Invalid button.', True)
                return
            if action in ('approve', 'reject'):
                target = self.user(value)
                if not target or target['role'] != 'pending' or not target['requested']:
                    self.answer(cb, 'This request has already been handled.', True)
                    return
                role = 'caller' if action == 'approve' else 'rejected'
                self.s.db.execute('UPDATE users SET role=? WHERE id=?', (role, value))
                self.event(uid, None, action, str(value))
                self.s.say(value, '✅ You’re approved! Send /lead to begin.' if role == 'caller' else 'Your access request was declined. Contact the bot owner.')
                self.s.say(uid, f'{"✅ Approved" if role == "caller" else "Rejected"}: {label(target)}')
                self.clear_buttons(cb)
            elif action == 'activate':
                file = self.s.one('SELECT * FROM files WHERE id=? AND name IS NOT NULL', (value,))
                if not file:
                    self.answer(cb, 'File not found.', True)
                    return
                self.s.set('active_file', value)
                self.event(uid, None, 'active_file_changed', str(value))
                self.s.say(uid, f'✅ Active file: {file["name"]}.\nNew /lead requests use this file. Existing assignments stay with their callers.')
                self.clear_buttons(cb)
            else:
                self.work(uid, value)
                self.clear_buttons(cb)
            self.answer(cb, 'Done')
            return
        if not self.allowed(uid):
            self.answer(cb, 'Access required. Send /start.', True)
            return
        if action == 'next':
            if self.s.get(f'upload:{uid}'):
                self.answer(cb, 'Name your uploaded file first, or send /cancel.', True)
                return
            self.lead(uid)
            self.answer(cb)
            return
        try:
            lid = int(data[1])
        except (IndexError, ValueError):
            self.answer(cb, 'Invalid button.', True)
            return
        lead = self.s.one('SELECT * FROM leads WHERE id=?', (lid,))
        if not lead or lead['caller_id'] != uid or lead['status'] not in OPEN_STATES:
            self.answer(cb, 'This lead is closed or is not assigned to you.', True)
            return
        state = lead['status']
        if action in ('picked', 'noanswer', 'invalid') and state == 'assigned':
            if action == 'picked':
                self.s.db.execute("UPDATE leads SET status='notes',picked_at=? WHERE id=?", (now(), lid))
                self.event(uid, lid, 'picked')
                self.s.say(uid, f'📝 Lead #{lid}: write your notes as one message. You’ll review them before sending.')
            else:
                status = 'no_answer' if action == 'noanswer' else 'invalid'
                self.s.db.execute('UPDATE leads SET status=?,completed_at=? WHERE id=?', (status, now(), lid))
                self.event(uid, lid, status)
                self.s.say(uid, f'{"📵 No Answer" if status == "no_answer" else "❌ Ran / Invalid"} recorded. '
                           'This lead is permanently closed.', self.next_button())
            self.clear_buttons(cb)
        elif action == 'send' and state == 'review':
            if len(data) != 3 or data[2] != str(lead['note_message_id']):
                self.answer(cb, 'Those notes were replaced. Send /lead to review your latest notes.', True)
                return
            self.s.db.execute("UPDATE leads SET status='decision' WHERE id=?", (lid,))
            self.event(uid, lid, 'notes_submitted')
            self.decision(uid, self.current(uid))
            self.clear_buttons(cb)
        elif action == 'editnotes' and state in ('review', 'decision'):
            self.s.db.execute("UPDATE leads SET status='notes' WHERE id=?", (lid,))
            self.s.say(uid, f'📝 Write replacement notes for lead #{lid}.')
            self.clear_buttons(cb)
        elif action in ('ready', 'ran') and state == 'decision':
            if action == 'ready':
                group = int(self.s.get('group_id', '0'))
                if not group:
                    self.answer(cb, 'The owner must connect the team group with /setgroup first. Your lead and notes are saved.', True)
                    return
                self.s.db.execute("UPDATE leads SET status='ready',completed_at=?,group_chat_id=? WHERE id=?", (now(), group, lid))
                updated = self.s.one('SELECT * FROM leads WHERE id=?', (lid,))
                self.s.say(group, self.handoff_text(updated), [[button('Take Further', f'take:{lid}')]],
                           kind='handoff', reference=lid, thread=int(self.s.get('group_thread', '0')))
                self.s.say(uid, f'✅ Lead #{lid} is Ready. The group handoff is queued for delivery.', self.next_button())
            else:
                self.s.db.execute("UPDATE leads SET status='ran',completed_at=? WHERE id=?", (now(), lid))
                self.s.say(uid, '❌ Ran recorded. This lead is permanently closed.', self.next_button())
            self.event(uid, lid, action)
            self.clear_buttons(cb)
        else:
            self.answer(cb, 'That button is from an earlier step. Send /lead to resume.', True)
            return
        self.answer(cb, 'Saved')

    def handoff_text(self, lead):
        file = self.s.one('SELECT name FROM files WHERE id=?', (lead['file_id'],))
        text = f'📁 {file["name"]}\n👤 {label(self.user(lead["caller_id"]))}\nHandoff #{lead["id"]}\n\n📝 {lead["notes"]}'
        if lead['claimed_by']:
            text += '\n\n✅ Taken by ' + label(self.user(lead['claimed_by']))
        return text

    def take(self, cb, uid, lid):
        if not self.admin(uid):
            self.answer(cb, 'Only bot admins can take this further.', True)
            return
        user = self.user(uid)
        if not user or not user['private_started']:
            self.answer(cb, 'Open the bot privately and send /start, then try again.', True)
            return
        lead = self.s.one('SELECT * FROM leads WHERE id=?', (lid,))
        msg = cb['message']
        if (not lead or msg['chat']['type'] not in ('group', 'supergroup') or
                msg['chat']['id'] != lead['group_chat_id'] or
                (lead['group_message_id'] and msg['message_id'] != lead['group_message_id'])):
            self.answer(cb, 'This is not the original handoff message.', True)
            return
        if lead['status'] != 'ready' or lead['claimed_by']:
            self.answer(cb, 'This handoff has already been claimed.', True)
            return
        self.s.db.execute("UPDATE leads SET status='claimed',claimed_by=?,claimed_at=? WHERE id=? AND status='ready' AND claimed_by IS NULL",
                          (uid, now(), lid))
        self.event(uid, lid, 'claimed')
        file = self.s.one('SELECT name FROM files WHERE id=?', (lead['file_id'],))
        self.s.say(uid, f'📁 {file["name"]}\nLead #{lid}\n👤 Caller: {label(self.user(lead["caller_id"]))}\n\n'
                   f'{lead["body"]}\n\n📝 Notes:\n{lead["notes"]}', kind='claim_details', reference=lid)
        updated = self.s.one('SELECT * FROM leads WHERE id=?', (lid,))
        self.s.queue('editMessageText', {'chat_id': msg['chat']['id'], 'message_id': msg['message_id'],
                     'text': self.handoff_text(updated), 'reply_markup': {'inline_keyboard': []},
                     'link_preview_options': {'is_disabled': True}})
        self.answer(cb, 'Claimed. Full details are being sent privately.')

    def manage_user(self, uid, cmd, argument):
        try:
            target_id = int(argument)
            target = self.user(target_id)
            if not target:
                raise ValueError
        except ValueError:
            self.s.say(uid, f'Use {cmd} TELEGRAM_USER_ID. The person must first send /start to this bot. Use /team or /pending to see IDs.')
            return
        if target_id == self.owner or target_id in self.config_admins:
            self.s.say(uid, 'This account is controlled by the owner/environment configuration.')
            return
        role = {'/addadmin': 'admin', '/removeadmin': 'caller', '/revoke': 'revoked', '/approve': 'caller'}[cmd]
        self.s.db.execute('UPDATE users SET role=? WHERE id=?', (role, target_id))
        self.event(uid, None, cmd[1:], str(target_id))
        self.s.say(uid, f'{label(target)} now has role: {role}.')
        self.s.say(target_id, f'Your bot access is now: {role}.' + (' Send /lead to begin.' if role in ('admin', 'caller') else ''))
        # Revoking access never silently recycles an assigned lead or reopens a closed one.
        if role == 'revoked' and self.current(target_id):
            self.s.say(uid, 'This user has an unfinished lead. It remains reserved and is recorded in /stats and /history.')

    def stats(self, chat, uid, personal=False, group=False):
        file_id = None if personal else int(self.s.get('active_file', '0'))
        file = self.s.one('SELECT name FROM files WHERE id=?', (file_id,)) if file_id else None
        where, args = ('WHERE l.caller_id=?', [uid]) if personal else ('WHERE l.file_id=?', [file_id])
        if not personal and not file:
            self.s.say(chat, 'No active file. Use /work to select one.')
            return
        rows = self.s.all(f'''SELECT u.id,u.username,u.name,
          SUM(CASE WHEN l.status IN ('ready','claimed') THEN 1 ELSE 0 END) AS ready,
          SUM(CASE WHEN l.status IN ('invalid','ran') THEN 1 ELSE 0 END) AS ran,
          SUM(CASE WHEN l.status='no_answer' THEN 1 ELSE 0 END) AS noanswer,
          SUM(CASE WHEN l.picked_at IS NOT NULL THEN 1 ELSE 0 END) AS picked,
          SUM(CASE WHEN l.status IN ('assigned','notes','review','decision') THEN 1 ELSE 0 END) AS progress
          FROM leads l JOIN users u ON u.id=l.caller_id {where} GROUP BY u.id ORDER BY ready DESC,u.id''', args)
        title = '📊 Your progress — all files' if personal else f'📊 {file["name"]} — Team Progress'
        lines = [title, '']
        for row in rows:
            lines.append(f'{label(row)} — ✅ {row["ready"]} · ❌ {row["ran"]} · 📵 {row["noanswer"]}')
            lines.append(f'   📞 Picked: {row["picked"]} · In progress: {row["progress"]}')
        if not rows:
            lines.append('No calls recorded yet.')
        if not personal:
            counts = {r['status']: r['n'] for r in self.s.all('SELECT status,COUNT(*) AS n FROM leads WHERE file_id=? GROUP BY status', (file_id,))}
            lines.append(f'\nAvailable: {counts.get("available", 0)} · In progress: {sum(counts.get(s,0) for s in OPEN_STATES)}')
        lines.append('\n✅ Ready (includes claimed) · ❌ Ran / Invalid · 📵 No Answer\nNo Answer and Ran / Invalid are permanently closed.')
        self.s.say(chat, '\n'.join(lines), thread=int(self.s.get('group_thread', '0')) if group else None)

    def history(self, uid, argument):
        try:
            lid = int(argument)
        except ValueError:
            self.s.say(uid, 'Use /history LEAD_NUMBER. Example: /history 12')
            return
        lead = self.s.one('SELECT * FROM leads WHERE id=?', (lid,))
        if not lead:
            self.s.say(uid, 'Lead not found.')
            return
        rows = self.s.all('SELECT * FROM audit WHERE lead_id=? ORDER BY id', (lid,))
        timeline = '\n'.join(f'{r["at"]} · {label(self.user(r["actor_id"]))} · {r["action"]}' for r in rows)
        self.s.say(uid, f'Lead #{lid} · {lead["status"]}\n\n{lead["body"]}\n\nNotes: {lead["notes"] or "none"}\n\nUTC timeline:\n{timeline}')

    def help(self, uid):
        text = ('/start — request access or open the bot\n/id — your Telegram user ID\n'
                '/lead — get or resume your lead\n/showlead — show your current lead details\n'
                '/mystats — your totals across all files\n/help — these instructions')
        if self.admin(uid):
            text += ('\n\nAdmin commands:\nUpload a .txt privately, then type its name.\n'
                     '/work — select the active file\n/files — list saved files\n'
                     '/stats — active-file team totals (also works in the connected group)\n'
                     '/pending — approve or reject requests\n/team — team roles and user IDs\n'
                     '/cancel — discard an unnamed upload\n/history NUMBER — full lead and audit timeline\n'
                     '/status — group, active file and failed notifications\n/retry — retry failed notifications')
        if uid == self.owner:
            text += ('\n\nOwner commands:\n/setgroup — run inside your team group/topic\n'
                     '/addadmin USER_ID — give admin access\n/removeadmin USER_ID — change admin to caller\n'
                     '/approve USER_ID — approve/reapprove a known user\n/revoke USER_ID — remove access')
        self.s.say(uid, text)
