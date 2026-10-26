# Team Workfile Lead Bot

## Admin
1. Run `/setgroup` in your review group.
2. Upload a `.txt` or `.csv` privately.
3. Bot asks `FILE NAME`.
4. Upload as many named files as you want.
5. Run `/selectwork`.
6. Tap the file the team should call today.

Only the selected file is used for `/lead`.

## Caller
- `/start` -> admin approval
- `/lead`
- PICKED / NO ANSWER / RAN / INVALID
- NO ANSWER returns at the end in later rounds
- RAN and INVALID never recirculate
- PICKED -> type notes -> READY FOR WA / RAN

## Upload parsing
- If `Fullname` exists, every occurrence starts a new lead.
- Otherwise every non-empty line is one lead.

## Railway
Use `TELEGRAM_BOT_TOKEN`.
Recommended persistent database:
`LEADS_DB_PATH=/data/team_workfiles.db`
Mount a Railway volume at `/data`.


## Lead separator rule

The bot supports exactly two automatic upload formats:

1. If `fullname` appears in the file, every occurrence of `fullname`
   starts a new lead. Everything until the next `fullname` belongs to
   the same lead.

2. If no `fullname` marker is found, every non-empty line is one lead.

Matching is case-insensitive and also accepts common variants such as
`Full Name`, `FULLNAME`, `full-name`, and `full_name`.


## PICKED group message

When a caller presses PICKED, the configured group receives:

🏦 FILE NAME
👤 CALLER
✅ PICKED CALL
⏰ UK DATE/TIME

The caller uses their Telegram @username when available.


## READY FOR WA + TAKE FURTHER

When a caller presses READY FOR WA, the review group gets only:

`📲 READY FOR WA`
`<caller notes>`

No lead information is posted in the group.

A `TAKE FURTHER` button is attached. Only current group admins can use it.
The first admin to claim it receives privately:

- file name
- full lead
- full notes

The claim is then marked as taken so another admin cannot claim the same lead.
