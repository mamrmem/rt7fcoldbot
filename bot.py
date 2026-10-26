import logging, os, re, sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
DB=os.getenv('LEADS_DB_PATH','team_workfiles.db').strip()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('team-workfile-bot')
FULLNAME=re.compile(r'(?i)\bfull[\s_\-:]*name\b')

def now(): return datetime.now(timezone.utc).isoformat()
def uk_time(): return datetime.now(ZoneInfo('Europe/London')).strftime('%d/%m/%Y %H:%M')
def dbc():
    c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c

def init_db():
    with dbc() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workers(user_id INTEGER PRIMARY KEY,username TEXT,full_name TEXT,status TEXT NOT NULL DEFAULT 'pending',requested_at TEXT NOT NULL,approved_at TEXT,approved_by INTEGER);
        CREATE TABLE IF NOT EXISTS work_files(id INTEGER PRIMARY KEY AUTOINCREMENT,display_name TEXT NOT NULL,source_filename TEXT,parser_mode TEXT,marker_count INTEGER NOT NULL DEFAULT 0,total_leads INTEGER NOT NULL DEFAULT 0,current_round INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,created_by INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY AUTOINCREMENT,work_file_id INTEGER NOT NULL,position INTEGER NOT NULL,value TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'available',assigned_to INTEGER,assigned_at TEXT,attempts INTEGER NOT NULL DEFAULT 0,last_round INTEGER NOT NULL DEFAULT 0,last_attempt_at TEXT,notes TEXT,caller_username TEXT,caller_name TEXT,ready_at TEXT,ran_at TEXT,invalid_at TEXT,taken_by INTEGER,taken_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_queue ON leads(work_file_id,status,assigned_to,last_round,position);
        ''')
        cols={r['name'] for r in c.execute('PRAGMA table_info(leads)').fetchall()}
        if 'taken_by' not in cols:c.execute('ALTER TABLE leads ADD COLUMN taken_by INTEGER')
        if 'taken_at' not in cols:c.execute('ALTER TABLE leads ADD COLUMN taken_at TEXT')

def set_setting(k,v):
    with dbc() as c: c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,str(v)))
def get_setting(k):
    with dbc() as c: r=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone()
    return r['value'] if r else None

def gid():
    try:return int(get_setting('team_group_id'))
    except:return None

def active_work():
    with dbc() as c:return c.execute('SELECT * FROM work_files WHERE active=1 ORDER BY id DESC LIMIT 1').fetchone()

def decode(raw):
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')):
        try:return raw.decode('utf-16')
        except:pass
    try:return raw.decode('utf-8-sig')
    except:return raw.decode('utf-8',errors='replace')

def parse(raw):
    t=decode(raw).replace('\x00','').replace('\r\n','\n').replace('\r','\n').replace('\u00a0',' ')
    ms=list(FULLNAME.finditer(t))
    if ms:
        vals=[]
        for i,m in enumerate(ms):
            e=ms[i+1].start() if i+1<len(ms) else len(t)
            v=t[m.start():e].strip()
            if v: vals.append(v)
        return vals,'FULLNAME_BLOCKS',len(ms)
    return [x.strip() for x in t.split('\n') if x.strip()],'LINE_PER_LEAD',0

async def is_admin(ctx,uid,chat=None):
    g=chat or gid()
    if not g:return False
    try:
        m=await ctx.bot.get_chat_member(g,uid)
        return m.status in (ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.OWNER)
    except TelegramError:return False

async def req_admin(u,c):
    if not gid():
        await u.effective_message.reply_text('No review group configured. Run /setgroup in your group.'); return False
    if not await is_admin(c,u.effective_user.id,gid()):
        await u.effective_message.reply_text('⛔ Admins only.'); return False
    return True

def worker(uid):
    with dbc() as c:return c.execute('SELECT * FROM workers WHERE user_id=?',(uid,)).fetchone()
async def req_worker(u):
    r=worker(u.effective_user.id)
    if r and r['status']=='approved':return True
    await u.effective_message.reply_text('⏳ You need admin approval first. Send /start.'); return False

def approval_kb(uid): return InlineKeyboardMarkup([[InlineKeyboardButton('✅ Approve',callback_data=f'approve:{uid}'),InlineKeyboardButton('❌ Decline',callback_data=f'decline:{uid}')]])
def lead_kb(i): return InlineKeyboardMarkup([[InlineKeyboardButton('PICKED ✅',callback_data=f'call:pickup:{i}'),InlineKeyboardButton('NO ANSWER 📵',callback_data=f'call:noanswer:{i}')],[InlineKeyboardButton('RAN ❌',callback_data=f'call:ran:{i}'),InlineKeyboardButton('INVALID 🚫',callback_data=f'call:invalid:{i}')]])
def notes_kb(i): return InlineKeyboardMarkup([[InlineKeyboardButton('READY FOR WA ✅',callback_data=f'notes:ready:{i}'),InlineKeyboardButton('RAN ❌',callback_data=f'notes:ran:{i}')]])
def next_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton('NEW LEAD ➡️',callback_data='newlead')]])
def take_kb(i): return InlineKeyboardMarkup([[InlineKeyboardButton('TAKE FURTHER ➡️',callback_data=f'take:{i}')]])
def select_kb():
    with dbc() as c: rows=c.execute('SELECT * FROM work_files ORDER BY id DESC').fetchall()
    return InlineKeyboardMarkup([[InlineKeyboardButton(('✅ ' if r['active'] else '')+f"{r['display_name']} ({r['total_leads']:,})",callback_data=f"selectwork:{r['id']}")] for r in rows])

async def setgroup(u,c):
    if u.effective_chat.type not in (ChatType.GROUP,ChatType.SUPERGROUP):
        await u.effective_message.reply_text('Run /setgroup inside your Telegram group.'); return
    if not await is_admin(c,u.effective_user.id,u.effective_chat.id):
        await u.effective_message.reply_text('⛔ Group admins only.'); return
    set_setting('team_group_id',u.effective_chat.id); await u.effective_message.reply_text('✅ Review group configured.')

async def start(u,c):
    if u.effective_chat.type!=ChatType.PRIVATE:return
    user=u.effective_user
    if gid() and await is_admin(c,user.id,gid()):
        a=active_work(); name=a['display_name'] if a else 'None'
        await u.message.reply_text(f"👑 <b>ADMIN</b>\n\nActive work: <b>{escape(name)}</b>\n\nUpload lead files anytime. After each upload I ask FILE NAME.\nUse /selectwork to choose today's file.",parse_mode='HTML'); return
    with dbc() as d:
        r=d.execute('SELECT * FROM workers WHERE user_id=?',(user.id,)).fetchone()
        if r and r['status']=='approved':
            d.execute('UPDATE workers SET username=?,full_name=? WHERE user_id=?',(user.username or '',user.full_name or '',user.id)); await u.message.reply_text("✅ You're approved. Send /lead."); return
        if r and r['status']=='pending': await u.message.reply_text('⏳ Approval pending.'); return
        if r:d.execute("UPDATE workers SET username=?,full_name=?,status='pending',requested_at=? WHERE user_id=?",(user.username or '',user.full_name or '',now(),user.id))
        else:d.execute("INSERT INTO workers(user_id,username,full_name,status,requested_at) VALUES(?,?,?,?,?)",(user.id,user.username or '',user.full_name or '','pending',now()))
    await u.message.reply_text('⏳ Approval request sent.')
    if gid():
        await c.bot.send_message(gid(),f"👤 <b>NEW CALLER</b>\n\nName: {escape(user.full_name or '')}\nUsername: {escape('@'+user.username if user.username else 'None')}",parse_mode='HTML',reply_markup=approval_kb(user.id))

async def approve_cb(u,c):
    q=u.callback_query; await q.answer()
    if not gid() or q.message.chat_id!=gid() or not await is_admin(c,u.effective_user.id,gid()):return
    action,ids=q.data.split(':'); uid=int(ids); ok=action=='approve'
    with dbc() as d:d.execute('UPDATE workers SET status=?,approved_at=?,approved_by=? WHERE user_id=?',('approved' if ok else 'declined',now() if ok else None,u.effective_user.id if ok else None,uid))
    try: await q.edit_message_reply_markup(reply_markup=None)
    except: pass
    await q.message.reply_text('✅ Approved.' if ok else '❌ Declined.')
    try: await c.bot.send_message(uid,"✅ You're approved. Send /lead." if ok else '❌ Your request was declined.')
    except: pass

async def upload(u,c):
    if u.effective_chat.type!=ChatType.PRIVATE or not await req_admin(u,c):return
    doc=u.message.document; fn=doc.file_name or 'leads.txt'
    if not fn.lower().endswith(('.txt','.csv')):
        await u.message.reply_text('Upload a .txt or .csv file.'); return
    f=await doc.get_file(); raw=bytes(await f.download_as_bytearray()); vals,mode,markers=parse(raw)
    if not vals: await u.message.reply_text('No leads detected.'); return
    c.user_data['pending_upload']={'filename':fn,'leads':vals,'mode':mode,'markers':markers}
    if mode=='FULLNAME_BLOCKS': await u.message.reply_text(f'✅ Fullname block format\nMarkers: {markers:,}\nLeads: {len(vals):,}\n\nFILE NAME')
    else: await u.message.reply_text(f'✅ Line-per-lead format\nLeads: {len(vals):,}\n\nFILE NAME')

async def text(u,c):
    if u.effective_chat.type!=ChatType.PRIVATE:return
    t=(u.message.text or '').strip()
    if not t or t.startswith('/'):return
    p=c.user_data.get('pending_upload')
    if p:
        if not await req_admin(u,c):return
        with dbc() as d:
            cur=d.execute('INSERT INTO work_files(display_name,source_filename,parser_mode,marker_count,total_leads,current_round,active,created_at,created_by) VALUES(?,?,?,?,?,1,0,?,?)',(t[:80],p['filename'],p['mode'],p['markers'],len(p['leads']),now(),u.effective_user.id)); wid=cur.lastrowid
            d.executemany("INSERT INTO leads(work_file_id,position,value,status,attempts,last_round) VALUES(?,?,?,'available',0,0)",[(wid,i,v) for i,v in enumerate(p['leads'],1)])
        c.user_data.pop('pending_upload',None)
        await u.message.reply_text(f"✅ <b>{escape(t[:80])}</b>\n{len(p['leads']):,} leads saved.\n\nUse /selectwork when you want the team to call it.",parse_mode='HTML'); return
    if not await req_worker(u):return
    w=active_work()
    if not w:return
    with dbc() as d:
        lead=d.execute("SELECT * FROM leads WHERE work_file_id=? AND assigned_to=? AND status='awaiting_notes' ORDER BY assigned_at DESC LIMIT 1",(w['id'],u.effective_user.id)).fetchone()
        if not lead:return
        d.execute('UPDATE leads SET notes=? WHERE id=?',(t,lead['id']))
    await u.message.reply_text('📝 Notes saved.',reply_markup=notes_kb(lead['id']))

async def selectwork(u,c):
    if not await req_admin(u,c):return
    with dbc() as d: n=d.execute('SELECT COUNT(*) c FROM work_files').fetchone()['c']
    if not n: await u.effective_message.reply_text('No uploaded work files yet.'); return
    await u.effective_message.reply_text("📁 <b>SELECT WORK</b>\n\nChoose today's lead file:",parse_mode='HTML',reply_markup=select_kb())

async def select_cb(u,c):
    q=u.callback_query; await q.answer()
    if not await req_admin(u,c):return
    fid=int(q.data.split(':')[1])
    with dbc() as d:
        r=d.execute('SELECT * FROM work_files WHERE id=?',(fid,)).fetchone()
        if not r:return
        d.execute('UPDATE work_files SET active=0'); d.execute('UPDATE work_files SET active=1 WHERE id=?',(fid,))
    await q.edit_message_text(f"✅ Active work: <b>{escape(r['display_name'])}</b>\n\nThe team will now receive leads from this file.",parse_mode='HTML')

def assign(user):
    d=dbc()
    try:
        d.execute('BEGIN IMMEDIATE'); w=d.execute('SELECT * FROM work_files WHERE active=1 ORDER BY id DESC LIMIT 1').fetchone()
        if not w: d.rollback(); return 'no_work',None,None
        ex=d.execute("SELECT * FROM leads WHERE work_file_id=? AND assigned_to=? AND status IN ('assigned','awaiting_notes') ORDER BY assigned_at DESC LIMIT 1",(w['id'],user.id)).fetchone()
        if ex: d.commit(); return 'existing',ex,w
        rnd=w['current_round']
        lead=d.execute("SELECT * FROM leads WHERE work_file_id=? AND status IN ('available','no_answer') AND assigned_to IS NULL AND last_round < ? ORDER BY position LIMIT 1",(w['id'],rnd)).fetchone()
        if not lead:
            retry=d.execute("SELECT COUNT(*) c FROM leads WHERE work_file_id=? AND status IN ('available','no_answer')",(w['id'],)).fetchone()['c']
            if retry==0: d.commit(); return 'finished',None,w
            busy=d.execute("SELECT COUNT(*) c FROM leads WHERE work_file_id=? AND status IN ('assigned','awaiting_notes')",(w['id'],)).fetchone()['c']
            if busy>0: d.commit(); return 'waiting',None,w
            rnd+=1; d.execute('UPDATE work_files SET current_round=? WHERE id=?',(rnd,w['id']))
            lead=d.execute("SELECT * FROM leads WHERE work_file_id=? AND status IN ('available','no_answer') AND assigned_to IS NULL AND last_round < ? ORDER BY position LIMIT 1",(w['id'],rnd)).fetchone()
        if not lead: d.commit(); return 'waiting',None,w
        d.execute("UPDATE leads SET status='assigned',assigned_to=?,assigned_at=?,caller_username=?,caller_name=? WHERE id=?",(user.id,now(),user.username or '',user.full_name or '',lead['id']))
        fresh=d.execute('SELECT * FROM leads WHERE id=?',(lead['id'],)).fetchone(); d.commit(); return 'assigned',fresh,w
    except: d.rollback(); raise
    finally:d.close()

async def lead_cmd(u,c):
    if u.effective_chat.type!=ChatType.PRIVATE or not await req_worker(u):return
    state,lead,w=assign(u.effective_user)
    if state=='no_work': await u.effective_message.reply_text('No work has been selected yet.'); return
    if state=='finished': await u.effective_message.reply_text('🎉 No unresolved leads left in the active work file.'); return
    if state=='waiting': await u.effective_message.reply_text('⏳ Try again shortly.'); return
    warn='⚠️ Finish this lead first.\n\n' if state=='existing' else ''
    await u.effective_message.reply_text(f"{warn}📞 <b>LEAD</b>\n\n<code>{escape(lead['value'])}</code>",parse_mode='HTML',reply_markup=lead_kb(lead['id']))

async def newlead(u,c):
    q=u.callback_query; await q.answer()
    try: await q.edit_message_reply_markup(reply_markup=None)
    except: pass
    await lead_cmd(u,c)

async def call_result(u,c):
    q=u.callback_query; await q.answer()
    if not await req_worker(u):return
    _,action,ids=q.data.split(':'); lid=int(ids); uid=u.effective_user.id
    d=dbc()
    try:
        d.execute('BEGIN IMMEDIATE'); w=d.execute('SELECT * FROM work_files WHERE active=1 ORDER BY id DESC LIMIT 1').fetchone(); lead=d.execute('SELECT * FROM leads WHERE id=?',(lid,)).fetchone()
        if not w or not lead or lead['work_file_id']!=w['id'] or lead['assigned_to']!=uid or lead['status']!='assigned': d.rollback(); return
        t=now()
        if action=='pickup': d.execute("UPDATE leads SET status='awaiting_notes',attempts=attempts+1,last_round=?,last_attempt_at=? WHERE id=?",(w['current_round'],t,lid))
        elif action=='noanswer': d.execute("UPDATE leads SET status='no_answer',assigned_to=NULL,assigned_at=NULL,attempts=attempts+1,last_round=?,last_attempt_at=? WHERE id=?",(w['current_round'],t,lid))
        elif action=='ran': d.execute("UPDATE leads SET status='ran',assigned_to=NULL,assigned_at=NULL,attempts=attempts+1,last_round=?,last_attempt_at=?,ran_at=? WHERE id=?",(w['current_round'],t,t,lid))
        else: d.execute("UPDATE leads SET status='invalid',assigned_to=NULL,assigned_at=NULL,attempts=attempts+1,last_round=?,last_attempt_at=?,invalid_at=? WHERE id=?",(w['current_round'],t,t,lid))
        d.commit()
    finally:d.close()
    try: await q.edit_message_reply_markup(reply_markup=None)
    except:pass
    if action=='pickup':
        g=gid()
        caller=f"@{u.effective_user.username}" if u.effective_user.username else (u.effective_user.full_name or str(u.effective_user.id))
        if g:
            try:
                await c.bot.send_message(
                    g,
                    f"🏦 <b>{escape(w['display_name'])}</b>\n"
                    f"👤 {escape(caller)}\n"
                    f"✅ <b>PICKED CALL</b>\n"
                    f"⏰ {escape(uk_time())}",
                    parse_mode='HTML'
                )
            except TelegramError:
                log.exception('Failed to post PICKED CALL to group')
        await q.message.reply_text('Upload Notes 📝')
    elif action=='noanswer': await q.message.reply_text('📵 NO ANSWER',reply_markup=next_kb())
    elif action=='ran': await q.message.reply_text('❌ RAN',reply_markup=next_kb())
    else: await q.message.reply_text('🚫 INVALID',reply_markup=next_kb())

async def notes_result(u,c):
    q=u.callback_query; await q.answer()
    if not await req_worker(u):return
    _,action,ids=q.data.split(':'); lid=int(ids); uid=u.effective_user.id
    with dbc() as d:
        lead=d.execute('SELECT * FROM leads WHERE id=?',(lid,)).fetchone()
        if not lead or lead['assigned_to']!=uid or lead['status']!='awaiting_notes':return
        if action=='ran':
            d.execute("UPDATE leads SET status='ran',assigned_to=NULL,assigned_at=NULL,ran_at=? WHERE id=?",(now(),lid))
            try: await q.edit_message_reply_markup(reply_markup=None)
            except: pass
            await q.message.reply_text('❌ RAN',reply_markup=next_kb())
            return
        notes=(lead['notes'] or '').strip()
        if not notes:
            await q.answer('Write notes first.',show_alert=True)
            return
        d.execute("UPDATE leads SET status='ready_wa',assigned_to=NULL,assigned_at=NULL,ready_at=? WHERE id=?",(now(),lid))

    g=gid()
    if not g:
        await q.answer('No review group configured.',show_alert=True)
        with dbc() as d:
            d.execute("UPDATE leads SET status='awaiting_notes',assigned_to=?,assigned_at=? WHERE id=?",(uid,now(),lid))
        return

    try:
        await c.bot.send_message(
            g,
            f"📲 <b>READY FOR WA</b>\n{escape(notes)}",
            parse_mode='HTML',
            reply_markup=take_kb(lid)
        )
    except TelegramError:
        log.exception('Failed to post READY FOR WA to group')
        with dbc() as d:
            d.execute("UPDATE leads SET status='awaiting_notes',assigned_to=?,assigned_at=? WHERE id=?",(uid,now(),lid))
        await q.answer('Could not post to group.',show_alert=True)
        return

    try: await q.edit_message_reply_markup(reply_markup=None)
    except: pass
    await q.message.reply_text('✅ READY FOR WA',reply_markup=next_kb())

async def take_further(u,c):
    q=u.callback_query; await q.answer()
    g=gid()
    if not g or q.message.chat_id!=g:return
    admin=u.effective_user
    if not await is_admin(c,admin.id,g):
        await q.answer('Admins only.',show_alert=True)
        return

    lid=int(q.data.split(':')[1])
    d=dbc()
    try:
        d.execute('BEGIN IMMEDIATE')
        lead=d.execute('SELECT * FROM leads WHERE id=?',(lid,)).fetchone()
        if not lead or lead['status']!='ready_wa':
            d.rollback()
            await q.answer('Already taken or unavailable.',show_alert=True)
            return
        work=d.execute('SELECT * FROM work_files WHERE id=?',(lead['work_file_id'],)).fetchone()
        d.execute("UPDATE leads SET status='taking',taken_by=? WHERE id=?",(admin.id,lid))
        d.commit()
    except Exception:
        d.rollback()
        raise
    finally:
        d.close()

    private=(
        f"🔥 <b>TAKE FURTHER</b>\n"
        f"🏦 <b>{escape(work['display_name'])}</b>\n"
        f"<code>{escape(lead['value'])}</code>\n"
        f"📝 {escape(lead['notes'] or '')}"
    )

    try:
        await c.bot.send_message(admin.id,private,parse_mode='HTML')
    except TelegramError:
        with dbc() as d:
            d.execute("UPDATE leads SET status='ready_wa',taken_by=NULL WHERE id=? AND status='taking'",(lid,))
        await q.answer('Open the bot privately and send /start first.',show_alert=True)
        return

    with dbc() as d:
        d.execute("UPDATE leads SET status='taken',taken_at=? WHERE id=? AND status='taking'",(now(),lid))

    try: await q.edit_message_reply_markup(reply_markup=None)
    except: pass

    who=f"@{admin.username}" if admin.username else (admin.full_name or str(admin.id))
    await q.message.reply_text(f"✅ Taken further by {escape(who)}.",parse_mode='HTML')

async def stats(u,c):
    if not await req_admin(u,c):return
    w=active_work()
    if not w: await u.effective_message.reply_text('No active work file.'); return
    with dbc() as d: rows=d.execute('SELECT status,COUNT(*) c FROM leads WHERE work_file_id=? GROUP BY status',(w['id'],)).fetchall()
    s={r['status']:r['c'] for r in rows}
    await u.effective_message.reply_text(f"📊 <b>{escape(w['display_name'])}</b>\n\nTotal: <b>{w['total_leads']:,}</b>\nAvailable: <b>{s.get('available',0):,}</b>\nNo Answer: <b>{s.get('no_answer',0):,}</b>\nAssigned: <b>{s.get('assigned',0):,}</b>\nWaiting Notes: <b>{s.get('awaiting_notes',0):,}</b>\nReady WA: <b>{s.get('ready_wa',0):,}</b>\nRan: <b>{s.get('ran',0):,}</b>\nInvalid: <b>{s.get('invalid',0):,}</b>",parse_mode='HTML')

def main():
    if not TOKEN: raise RuntimeError('Set TELEGRAM_BOT_TOKEN')
    init_db(); app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('start',start)); app.add_handler(CommandHandler('setgroup',setgroup)); app.add_handler(CommandHandler('lead',lead_cmd)); app.add_handler(CommandHandler('selectwork',selectwork)); app.add_handler(CommandHandler('stats',stats))
    app.add_handler(CallbackQueryHandler(approve_cb,pattern=r'^(approve|decline):\d+$')); app.add_handler(CallbackQueryHandler(select_cb,pattern=r'^selectwork:\d+$')); app.add_handler(CallbackQueryHandler(call_result,pattern=r'^call:(pickup|noanswer|ran|invalid):\d+$')); app.add_handler(CallbackQueryHandler(notes_result,pattern=r'^notes:(ready|ran):\d+$')); app.add_handler(CallbackQueryHandler(newlead,pattern=r'^newlead$')); app.add_handler(CallbackQueryHandler(take_further,pattern=r'^take:\d+$'))
    app.add_handler(MessageHandler(filters.Document.ALL,upload)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text))
    log.info('Team Workfile Lead Bot running'); app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=='__main__': main()
