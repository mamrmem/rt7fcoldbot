# Telegram Lead Team Bot

A Python Telegram bot for assigning leads to callers and passing qualified
leads to admins. The team makes the phone calls itself; this bot distributes
records, collects outcomes and notes, and tracks progress.

**For GitHub and Render setup, read [START_HERE.md](START_HERE.md).**

## The agreed workflow

- An admin privately uploads a `.txt` file, then sends its display name.
  The bot saves it. More files can be uploaded and named separately.
- `/work` lists saved files with available counts. Selecting one makes it the
  active file for everyone's new lead requests. Uploading a file alone does
  not activate it.
- Admins can use `/lead` too. An existing assignment stays with its caller when
  the active file changes. `/lead` resumes an unfinished lead rather than
  assigning several at once.
- `/start` from a new caller notifies the owner with Approve/Reject buttons.
- `/lead` sends one full lead privately, with Picked, No Answer and Ran / Invalid.
- No Answer and Ran / Invalid permanently close the lead and offer Next Lead.
- Picked asks for notes. The caller writes one message, reviews it and presses
  Send. Then they choose Ready or Ran. Ran permanently closes the lead.
- Ready posts file name, caller and notes in the connected group, with Take
  Further. The bot does not append the full lead record to the group message.
- Only a bot admin can claim the handoff. Its full lead and notes go privately
  to that admin, and the group message shows who claimed it. Claiming is atomic.
- SQLite saves uploads, active file, roles, assignments, outcomes, notes, claim
  ownership, timestamps, audit events, Telegram cursor and outgoing delivery work.

## Lead file format

Upload `.txt` files as Telegram documents, smaller than 19 MB each. Start each
record at the beginning of a line with `fullname:`. Everything up to the next
marker belongs to that lead. Blank lines between records are optional.

```text
fullname: Alex Example
phone: 07700 900001
business: Example Plumbing
city: London
any extra fields: stay with this lead

fullname: Blair Example
phone: 07700 900002
business: Example Landscaping
```

Markers are case-insensitive. `Full Name:`, `full_name:` and `fullname =` also
work. A marker mentioned inside a notes line does not split a record. Remove
headings or other text before the first marker; the bot rejects those files
rather than silently dropping the header. UTF-8 is recommended. UTF-8 BOM,
UTF-16 BOM and Windows-1252 text are also accepted. Limits: 100,000 leads per
file, and 40,000 UTF-16 text units per lead. Long records are split into several
private Telegram messages, with the outcome buttons on the last message.

File names are unique, one line and at most 80 UTF-16 units. Upload one file at
a time per admin: name it before sending the next. `/cancel` discards an
unnamed upload; it does not cancel an assigned lead.

Identical complete records are skipped across all saved files, ignoring case,
blank lines and whitespace differences. This prevents an exact duplicate upload
from reopening a permanently closed lead. The bot reports how many were skipped.
Different records sharing a phone number are not automatically merged.

## Commands

Caller commands, in private chat:

| Command | Action |
|---|---|
| `/start` | Request approval, or reopen the bot |
| `/id` | Show your numeric Telegram user ID |
| `/lead` | Get a lead, or resume your existing lead |
| `/next` | Same safe behavior as `/lead`; cannot skip an unfinished lead |
| `/showlead` | Show the full current record again while writing notes |
| `/mystats` | Your totals across all files |
| `/help` | Show available commands |

Bot admin commands, in private chat:

| Command | Action |
|---|---|
| Upload a `.txt`, then type a name | Save a named batch |
| `/work` or `/files` | Paginated batch list; choose the active file |
| `/stats` | Team totals for the active file |
| `/pending` | Outstanding approval requests with buttons |
| `/team` | Approved callers/admins and numeric IDs |
| `/cancel` | Discard your unnamed upload |
| `/history 12` | Full record, latest notes and UTC audit timeline for lead 12 |
| `/status` | Connected group, active file and failed notification count |
| `/retry` | Retry failed notifications after fixing delivery permissions |

Owner-only commands:

| Command | Action |
|---|---|
| `/setgroup` | Run inside the team group/topic to connect it |
| `/addadmin 123456789` | Give a known user bot-admin access |
| `/removeadmin 123456789` | Change a bot admin to an approved caller |
| `/approve 123456789` | Approve or reapprove a known user |
| `/revoke 123456789` | Remove access to bot actions |

The person must first contact the bot. Numeric IDs appear in access requests,
`/pending`, `/team` and their own `/id`. Bot admins can see all lead records and
manage files and approvals. Group administrators are not automatically bot
admins. Only the owner changes bot-admin roles or connects the group.

Inside the connected group, bot admins can send `/stats` or
`/stats@YourBotUsername`. Other lead operations happen privately.

## Statistics

```text
📊 London Leads — Team Progress

@gorgesmiht — ✅ 14 · ❌ 25 · 📵 8
   📞 Picked: 20 · In progress: 1

Available: 74 · In progress: 2
```

- ✅ counts Ready leads, including those subsequently claimed by an admin.
- ❌ counts Ran / Invalid, including Ran after notes.
- 📵 counts No Answer, permanently closed.
- 📞 Picked counts leads on which Picked was pressed, including later outcomes.
- In progress counts reserved leads still awaiting an outcome.

`/stats` covers the active file. `/mystats` covers the requesting person's
assigned leads across all files. Times in the audit log are UTC. The group
does not receive a new scoreboard after every failed call; an admin requests
the current scoreboard with `/stats`.

## Local development

Requires Python 3.11 or newer. No third-party package installation is needed.

```sh
cp .env.example .env
# Edit .env: add TELEGRAM_BOT_TOKEN and OWNER_USER_ID.
python3 bot.py
```

If you do not know your owner ID, start with just the token, privately send
`/id`, stop the bot, fill in `OWNER_USER_ID`, and start again. Lead features
remain locked until the owner is set. This avoids an unsafe first-user-wins
owner setup.

For Docker or another host, run `python -u bot.py`, set the same environment
variables, and mount a persistent volume at the directory containing the DB.
The included Dockerfile defaults to `/data/leads.sqlite3`; mount a volume at
`/data`. Do not expose an HTTP port: this is a polling background worker.

Optional environment variables:

- `ADMIN_USER_IDS`: comma-separated numeric IDs of additional bot admins.
  Those permissions are controlled by the environment, so remove them there
  to revoke them; `/removeadmin` handles admins added through the bot.
- `TEAM_GROUP_ID`: seed a numeric group ID on first run. `/setgroup` is easier
  and also supports a forum topic. A subsequently saved group takes priority.
- `DATABASE_PATH`: defaults locally to `data/leads.sqlite3`. On Render it must
  be `/var/data/leads.sqlite3`, on a disk mounted at `/var/data`.

The bot reads a local `.env` file if present. Real environment settings take
priority. `.gitignore` excludes tokens, databases and backups.

## Delivery and restart behavior

Assignment and outcome changes use SQLite transactions. A partial unique index
prevents more than one open lead per caller. The code reserves an available
record under a write lock, and verifies the caller and state on every button
press. Old buttons cannot overwrite a completed outcome. Edited notes require
the newest review button before submitting.

Updates and their cursor are committed alongside queued outgoing messages.
Telegram network failures leave the queue available for retry. A permanently
failed handoff or admin DM is recorded and, when possible, the owner is notified.
Fix permissions or unblock/start the bot privately, then use `/retry`.

An admin's claim remains assigned to that admin even if their DM fails; the
full record is never sent to the group as a fallback. A caller marked Ready
is told that the group handoff is queued, rather than promised delivery before
Telegram has accepted it. Use `/status` to check delivery failures.

Telegram sendMessage has no idempotency key. In the narrow case where Telegram
accepts a message but the connection/process fails before the delivery is saved,
a retry can produce a duplicate message. Assignment/closure/claim state remains
single and cannot be reopened by those copies. The stored canonical handoff
message controls later claims.

Only run one polling instance and one replica. A process lock prevents multiple
copies using the same database. Telegram rejects another poller using the same
token on a different host. Stop the previous Railway/shuffler/local process.
No Answer, Invalid and Ran records are never automatically requeued. Unfinished
assignments also never expire automatically: this avoids unexpectedly assigning
a record to two people. Revoking a caller preserves their unfinished assignment;
reapprove them to let them complete it. There is no delete/reset/reopen command.

The bot saves all lead text in its database. The group gets caller-supplied notes,
so anything typed into those notes will appear there. Keep original uploads in
private admin chat. The application logs technical events without printing
tokens, full lead bodies or note text to stdout.

## Backups

The database is the source of truth. Take consistent backups with the included
SQLite backup utility; do not copy only the live DB file while WAL is active.

```sh
python backup.py /var/data/leads.sqlite3 /var/data/backups/leads-2026-10-22.sqlite3
```

Choose a new backup name each time. Copy backups to separate storage if you need
protection against losing the hosting volume itself. To restore, stop the bot,
move the old database and its `-wal`/`-shm` sidecar files aside, copy the backup to
the configured database filename, then restart. A restore returns all progress
to the backup time. Keep the token and owner configuration separately.

## Testing

```sh
python -m unittest discover -s tests -v
```

Tests use temporary databases, invented sample records and a fake Telegram API.
They cover parser boundaries/encodings, approvals and roles, concurrent allocation,
state transitions, permanent closure, file selection, note revisions, hidden
group data, exclusive admin handoffs, duplicate updates, restart persistence,
score totals and failed/retried delivery. GitHub Actions runs them on Python
3.11 and 3.13 after upload. The package has not been connected to your live bot;
follow the real-bot check in `START_HERE.md` after you enter your own token.

## Troubleshooting

- **Render asks for a port:** you created a Web Service. Create a Background Worker.
- **Persistent disk startup error:** mount a disk at `/var/data`; set
  `DATABASE_PATH=/var/data/leads.sqlite3`. Render worker disks require a paid plan.
- **Bot shows setup text:** set your numeric `OWNER_USER_ID` and redeploy.
- **No leads found:** check the beginning-of-line `fullname:` markers.
- **No available leads:** name the uploaded batch, then select it with `/work`.
- **Ready says no group:** the owner must send `/setgroup` inside the group.
- **Take Further refuses a group admin:** the owner must add them with `/addadmin`.
- **Admin cannot get details:** they must privately `/start` and unblock the bot.
  Fix that, then have an admin use `/retry`.
- **Telegram code 409:** stop every other process polling the same bot token.
- **Existing webhook:** use a new token from BotFather or explicitly remove the
  old bot's webhook first. This app never silently deletes a configured webhook.
- **No reply to a group command:** address it to the bot, e.g.
  `/stats@YourBotUsername`. Telegram privacy mode can remain enabled.

Official service references:
[Telegram Bot API](https://core.telegram.org/bots/api),
[Render Background Workers](https://render.com/docs/background-workers),
[Render Persistent Disks](https://render.com/docs/disks),
[Render Blueprint specification](https://render.com/docs/blueprint-spec).
