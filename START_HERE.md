# Your Telegram lead team bot

## 1. Upload the code to GitHub

Unzip this package. Open the extracted `telegram-lead-team-bot` folder.
In your GitHub repository choose **Add file → Upload files**. Upload the
**contents of that folder**, so `bot.py`, `app.py`, `storage.py` and `render.yaml`
appear at the top level of the repository. Commit the upload.

Use a new repository for this bot, or replace the previous shuffler's files.
This is a new calling workflow and does not import the shuffler's configuration.
Keep the included `tests`, `examples` and `.github` folders with the code.
There is no `package.json`: this is Python and needs no third-party packages.

## 2. Create the Telegram bot

In Telegram open **@BotFather**, send `/newbot`, and follow the prompts.
Copy the token. You will enter it in Render's Environment settings.
You can reuse an existing bot token if you stop its old Railway/Render/local
process first. Only one copy may poll Telegram with a particular token.

## 3. Create a Render Background Worker

In Render choose **+ New → Background Worker**, connect GitHub, and select the
repository. Enter:

| Field | Value |
|---|---|
| Language/runtime | Python 3 |
| Branch | Your uploaded branch, usually `main` |
| Root Directory | Leave empty if the files are at the repository root |
| Build Command | `python -m compileall -q bot.py app.py storage.py backup.py` |
| Start Command | `python -u bot.py` |
| Instance | A paid worker; one instance only |

Add these **environment variables**:

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your token from BotFather |
| `DATABASE_PATH` | `/var/data/leads.sqlite3` |
| `OWNER_USER_ID` | Your numeric Telegram user ID, if you already know it |

Add a **persistent disk** in the creation form's Advanced section, or on the
worker's **Disks** page:

- Mount path: `/var/data`
- Size: `1 GB` initially

Deploy. The worker and disk are paid Render resources. The bot deliberately
refuses to run on Render without the persistent disk, because otherwise
redeploying would lose your leads and progress.

You can alternatively choose **New → Blueprint** and select this repo:
the included `render.yaml` supplies the worker, disk and commands. Enter your
bot token when prompted. Add `OWNER_USER_ID` after the ID step below if needed.

## 4. Set yourself as owner

If you do not know your numeric Telegram ID, leave `OWNER_USER_ID` unset for the
first deploy. Open **your bot privately** and send `/id`. It replies with your
numeric user ID.

In Render → your worker → **Environment**, add `OWNER_USER_ID` with that number,
then save and redeploy. Send `/start` privately to the bot again.

Until the owner ID is configured, the bot only shows setup information; nobody
can claim ownership by messaging it first. Use a numeric ID, not an `@username`.
After setup, ownership is saved in the database. Changing that environment ID
to a different person intentionally requires an explicit database migration.

## 5. Connect your team group

Add the bot to your Telegram group and allow it to send messages. From **your
own Telegram account**, send `/setgroup` in that group. If Telegram privacy mode
does not deliver the bare command, use `/setgroup@YourBotUsername`.
For a forum group, run it inside the topic where you want handoffs to appear.

You should see: **“This group/topic is connected.”**
Use your personal account for setup, not anonymous “send as group” mode.
Telegram group-admin status alone does not grant bot-admin access.

## 6. Add files and begin

1. Privately upload `examples/sample-leads.txt` to test.
2. When asked, type a file name, e.g. `London Leads`.
3. Repeat with other files and names.
4. Send `/work` and press a file to make it active.
5. Send `/lead` yourself to test the calling buttons.
6. Ask each caller to privately `/start` the bot. Approve their requests.
7. To add another bot admin, have them `/start`, then send
   `/addadmin THEIR_NUMERIC_USER_ID` privately as owner. `/team` and `/pending`
   show user IDs.

Original lead files should be uploaded **privately**. Your group receives only
Ready handoffs with file name, caller and notes. A bot admin claims a handoff
with **Take Further** and gets the full record privately.

## 7. Quick real-bot check

- Let a second account request access; approve it as owner.
- Request a lead, choose **No Answer**, then **Next Lead**. The old record must
  not return.
- Choose **Picked** on another lead; write notes, press **Send**, then **Ready**.
- Check the group message contains only the file name, caller and notes.
- Try **Take Further** as a caller: it should refuse. Try as an admin: full
  details should arrive privately and the group message should show who took it.
- Send `/stats` in the group as admin, and restart/redeploy the worker to check
  the saved file, totals and assignments remain.

See `README.md` for all commands, file formatting, backups and troubleshooting.
