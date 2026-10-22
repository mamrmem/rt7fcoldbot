#!/usr/bin/env python3
"""Run one polling instance. No external Python dependencies."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app import App
from storage import MAX_FILE_BYTES, Store

LOG = logging.getLogger('lead_team_bot')
STOP = False


class APIError(RuntimeError):
    def __init__(self, code=0, retry_after=0, description=''):
        self.code, self.retry_after, self.description = code, retry_after, description
        super().__init__(f'Telegram request failed (code {code}).')

    @property
    def transient(self):
        return self.code == 0 or self.code == 429 or self.code >= 500


class Telegram:
    def __init__(self, token):
        self.root = f'https://api.telegram.org/bot{token}/'
        self.file_root = f'https://api.telegram.org/file/bot{token}/'

    def call(self, method, payload=None, timeout=45):
        request = Request(self.root + method, data=json.dumps(payload or {}).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        code = 0
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(25_000_000)
        except HTTPError as error:
            code = error.code
            with error:
                raw = error.read(10_000)
        except (URLError, OSError, TimeoutError):
            raise APIError() from None
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise APIError(code) from None
        if not isinstance(body, dict) or not body.get('ok'):
            body = body if isinstance(body, dict) else {}
            raise APIError(body.get('error_code', code), body.get('parameters', {}).get('retry_after', 0),
                           body.get('description', ''))
        return body.get('result')

    def download(self, file_id):
        try:
            info = self.call('getFile', {'file_id': file_id})
        except APIError as error:
            if not error.transient:
                raise ValueError('Telegram could not provide that file. Upload a fresh .txt document smaller than 19 MB.') from None
            raise
        if info.get('file_size', 0) > MAX_FILE_BYTES:
            raise ValueError('Upload a .txt file smaller than 19 MB.')
        path = info.get('file_path')
        if not path:
            raise ValueError('Telegram could not provide that file. Upload it again.')
        try:
            with urlopen(self.file_root + quote(path, safe='/'), timeout=60) as response:
                raw = response.read(MAX_FILE_BYTES + 1)
        except HTTPError as error:
            if error.code in (400, 403, 404):
                raise ValueError('That file is no longer available. Please upload it again.') from None
            raise APIError(error.code) from None
        except (URLError, OSError, TimeoutError):
            raise APIError() from None
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError('Upload a .txt file smaller than 19 MB.')
        return raw


def deliver(store, api, owner, limit=30):
    """Persist outgoing work so a transient outage never discards a handoff."""
    if time.time() < float(store.get('telegram_paused_until', '0')):
        return
    rows = store.all('SELECT * FROM outbox WHERE sent=0 AND next_try<=? ORDER BY id LIMIT ?', (time.time(), limit))
    for row in rows:
        if STOP:
            break
        payload = json.loads(row['payload'])
        try:
            result = api.call(row['method'], payload)
        except APIError as error:
            with store.transaction():
                attempts = row['attempts'] + 1
                if row['method'] == 'answerCallbackQuery' or (row['method'].startswith('editMessage') and not error.transient):
                    # Stale messages/buttons cannot undo an authoritative DB transition.
                    store.db.execute('UPDATE outbox SET sent=1,payload=? WHERE id=?', ('{}', row['id']))
                elif error.transient:
                    delay = max(error.retry_after, min(300, 2 ** min(attempts, 8)))
                    store.db.execute('UPDATE outbox SET attempts=?,next_try=? WHERE id=?', (attempts, time.time() + delay, row['id']))
                    if error.code == 429:
                        store.set('telegram_paused_until', time.time() + delay)
                else:
                    store.db.execute('UPDATE outbox SET sent=-1,attempts=? WHERE id=?', (attempts, row['id']))
                    store.audit(None, row['reference_id'], 'notification_failed', f'outbox={row["id"]};code={error.code};kind={row["kind"]}')
                    # Do not recursively notify an unreachable owner.
                    if owner and payload.get('chat_id') != owner:
                        store.say(owner, f'⚠️ Notification delivery failed (Telegram code {error.code}).\n'
                                  f'Reference: {row["reference_id"] or row["id"]}.\n'
                                  'Check that the bot can post in the group and that admins have /start-ed it privately. '
                                  'Use /status, then /retry after fixing access. Lead state and notes remain saved.')
            if error.code == 401:
                raise
            if error.code == 429:
                break
        else:
            with store.transaction():
                store.db.execute('UPDATE outbox SET sent=1,payload=? WHERE id=?', ('{}', row['id']))
                if row['kind'] == 'handoff' and isinstance(result, dict):
                    store.db.execute('UPDATE leads SET group_message_id=? WHERE id=?', (result['message_id'], row['reference_id']))
                    store.audit(None, row['reference_id'], 'handoff_delivered')
                elif row['kind'] == 'claim_details':
                    store.audit(None, row['reference_id'], 'claim_details_delivered')


def load_env(path=Path('.env')):
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                os.environ.setdefault(key.strip(), value)


def positive_id(value, name):
    if not value:
        return 0
    try:
        result = int(value)
    except ValueError:
        raise ValueError(f'{name} must be a numeric Telegram user ID.') from None
    if result <= 0:
        raise ValueError(f'{name} must be a positive Telegram user ID.')
    return result


def lock_instance(path):
    """Keep a process-wide lock on the same persistent volume as SQLite."""
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError('Another bot process is using this database. Stop it before starting a second copy.') from None
    return handle


def persistent_mount(path):
    if os.path.ismount(path):
        return True
    # os.path.ismount alone misses bind mounts on the same filesystem.
    try:
        return any(line.split()[4] == path for line in Path('/proc/self/mountinfo').read_text().splitlines())
    except (OSError, IndexError):
        return False


def stop(_signum, _frame):
    global STOP
    STOP = True


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    load_env()
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Set TELEGRAM_BOT_TOKEN to the token from @BotFather.')
    owner = positive_id(os.environ.get('OWNER_USER_ID', '').strip(), 'OWNER_USER_ID')
    admins = [positive_id(item.strip(), 'ADMIN_USER_IDS') for item in os.environ.get('ADMIN_USER_IDS', '').split(',') if item.strip()]
    try:
        group = int(os.environ.get('TEAM_GROUP_ID', '0') or '0')
    except ValueError:
        raise ValueError('TEAM_GROUP_ID must be a numeric group chat ID, or left blank.') from None
    db_path = Path(os.environ.get('DATABASE_PATH', 'data/leads.sqlite3')).expanduser().resolve()
    if os.environ.get('RENDER') and not str(db_path).startswith('/var/data/'):
        raise ValueError('On Render, set DATABASE_PATH=/var/data/leads.sqlite3 and attach a persistent disk at /var/data.')
    if os.environ.get('RENDER') and not persistent_mount('/var/data'):
        raise ValueError('No persistent disk is mounted at /var/data. Add one before starting the bot.')
    db_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = lock_instance(str(db_path) + '.lock')
    store = Store(db_path)
    api = Telegram(token)
    app = App(store, api, owner, admins, group)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    delay = 1
    try:
        me = api.call('getMe')
        webhook = api.call('getWebhookInfo')
        if webhook.get('url'):
            raise ValueError('This token has a webhook. Use a new BotFather bot or remove its existing webhook before polling.')
        LOG.info('Running @%s with durable database. One polling instance only.', me.get('username', 'bot'))
        if not app.owner:
            LOG.info('Owner setup pending: privately send /id, set OWNER_USER_ID, then redeploy. All lead features are locked until then.')
        while not STOP:
            try:
                deliver(store, api, app.owner)
                # Shorter poll when outgoing messages are waiting for a retry.
                waiting = store.one('SELECT 1 FROM outbox WHERE sent=0 LIMIT 1')
                updates = api.call('getUpdates', {'offset': int(store.get('offset', '0')),
                    'timeout': 2 if waiting else 20, 'allowed_updates': ['message', 'callback_query']}, timeout=30)
                for update in updates:
                    if STOP:
                        break
                    app.process(update)
                    deliver(store, api, app.owner)
                delay = 1
            except APIError as error:
                if error.code in (401, 404, 409):
                    raise
                LOG.warning('Telegram temporarily unavailable (code %s). Retrying; saved work is preserved.', error.code)
                interruptible_wait(max(delay, error.retry_after))
                delay = min(delay * 2, 60)
            except (sqlite3.Error, OSError) as error:
                LOG.error('Storage problem (%s). Update was not acknowledged; retrying.', type(error).__name__)
                interruptible_wait(10)
            except Exception as error:
                # Never log raw updates, tokens, full lead records or note text.
                LOG.error('Update handling failed (%s). Saved state is retained; retrying.', type(error).__name__)
                interruptible_wait(10)
    finally:
        store.close()
        process_lock.close()


def interruptible_wait(seconds):
    deadline = time.monotonic() + seconds
    while not STOP and time.monotonic() < deadline:
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))


if __name__ == '__main__':
    try:
        main()
    except APIError as error:
        LOG.error('Telegram refused startup/polling (code %s). Check the token; stop any other bot instance using it.', error.code)
        sys.exit(1)
    except (ValueError, OSError, sqlite3.Error) as error:
        # Configuration errors are deliberately constructed without secrets.
        LOG.error('%s', str(error) if isinstance(error, ValueError) else type(error).__name__)
        sys.exit(1)
