"""Durable state and lead parsing. Python standard library only."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_FILE_BYTES = 19_000_000
MAX_LEADS = 100_000
MAX_LEAD_UNITS = 40_000
FULLNAME = re.compile(r'^\s*["\']?full[ _-]*name["\']?\s*(?::|=|\t|\s+|$)', re.I)
STATES = ('available', 'assigned', 'notes', 'review', 'decision',
          'no_answer', 'invalid', 'ran', 'ready', 'claimed')
OPEN_STATES = ('assigned', 'notes', 'review', 'decision')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def units(text):
    return len(text.encode('utf-16-le')) // 2


def chunks(text, limit=3500):
    """Telegram limits text by UTF-16 units, including astral emoji."""
    current, size = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            yield ''.join(current)
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        yield ''.join(current)


def parse_leads(raw):
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError('Upload a non-empty .txt file smaller than 19 MB.')
    try:
        text = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
    except UnicodeError:
        try:
            text = raw.decode('cp1252')
        except UnicodeError:
            raise ValueError('Save your file as UTF-8 text, then upload it again.') from None
    if '\x00' in text:
        raise ValueError('This does not look like a text file. Save it as UTF-8 .txt.')
    records, current, preamble = [], [], []
    for line in text.splitlines():
        if FULLNAME.match(line):
            if current:
                records.append('\n'.join(current).strip())
            current = [line]
        elif current:
            current.append(line)
        elif line.strip():
            preamble.append(line)
    if current:
        records.append('\n'.join(current).strip())
    if not records:
        raise ValueError('No leads found. Start each lead on a new line with fullname: Name.')
    if preamble:
        raise ValueError('Text appears before the first fullname. Remove that header and upload again; nothing was imported.')
    if len(records) > MAX_LEADS:
        raise ValueError('Split this file into batches of at most 100,000 leads.')
    if any(units(record) > MAX_LEAD_UNITS for record in records):
        raise ValueError('A lead is over 40,000 characters. Check your fullname separators.')
    return records


def fingerprint(record):
    # Normalize case, spaces and line endings so reuploads cannot reopen records.
    normalized = '\n'.join(' '.join(line.split()).casefold() for line in record.splitlines() if line.strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


class Store:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.execute('PRAGMA journal_mode = WAL')
        self.db.execute('PRAGMA synchronous = FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, username TEXT, name TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'pending', requested INTEGER NOT NULL DEFAULT 0,
          private_started INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
          id INTEGER PRIMARY KEY, name TEXT COLLATE NOCASE UNIQUE,
          original_name TEXT NOT NULL, owner_id INTEGER NOT NULL,
          skipped INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS leads(
          id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id),
          body TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'available', caller_id INTEGER REFERENCES users(id),
          notes TEXT, note_message_id INTEGER, picked_at TEXT,
          assigned_at TEXT, completed_at TEXT, claimed_by INTEGER REFERENCES users(id),
          claimed_at TEXT, group_chat_id INTEGER, group_message_id INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_lead_per_caller ON leads(caller_id)
          WHERE status IN ('assigned','notes','review','decision');
        CREATE INDEX IF NOT EXISTS lead_queue ON leads(file_id,status,id);
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor_id INTEGER,
          lead_id INTEGER REFERENCES leads(id), action TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY, method TEXT NOT NULL, payload TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT '', reference_id INTEGER,
          attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
          sent INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent,next_try,id);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def get(self, key, default=None):
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return row['value'] if row else default

    def set(self, key, value):
        self.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def audit(self, actor, lead, action, detail=''):
        self.db.execute('INSERT INTO audit(at,actor_id,lead_id,action,detail) VALUES(?,?,?,?,?)',
                        (now(), actor, lead, action, detail))

    def queue(self, method, payload, kind='', reference=None):
        self.db.execute('INSERT INTO outbox(method,payload,kind,reference_id,created) VALUES(?,?,?,?,?)',
                        (method, json.dumps(payload, ensure_ascii=False), kind, reference, now()))

    def say(self, chat, text, keyboard=None, kind='', reference=None, thread=None):
        parts = list(chunks(text))
        for index, part in enumerate(parts):
            payload = {'chat_id': chat, 'text': part, 'link_preview_options': {'is_disabled': True}}
            if thread:
                payload['message_thread_id'] = thread
            if index == len(parts) - 1 and keyboard:
                payload['reply_markup'] = {'inline_keyboard': keyboard}
            self.queue('sendMessage', payload, kind if index == len(parts) - 1 else '', reference)

    def close(self):
        self.db.close()
