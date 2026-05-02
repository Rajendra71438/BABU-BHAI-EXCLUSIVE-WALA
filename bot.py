import os
import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from functools import partial

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.error import Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatJoinRequestHandler,
    ContextTypes, filters,
)

# ============ CONFIG ============
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", "0"))

BROADCAST_DELAY = 0.5
DB_PATH = "bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============ DATABASE ============
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen DATE NOT NULL DEFAULT (DATE('now'))
            );
            CREATE TABLE IF NOT EXISTS subadmins (
                user_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'subadmin',
                added_at TIMESTAMP DEFAULT (DATETIME('now'))
            );
            CREATE TABLE IF NOT EXISTS subadmin_perms (
                user_id INTEGER PRIMARY KEY,
                can_broadcast INTEGER DEFAULT 1,
                can_stats INTEGER DEFAULT 1,
                can_manage_seq INTEGER DEFAULT 0,
                can_change_source INTEGER DEFAULT 0,
                can_set_post_button INTEGER DEFAULT 0,
                can_manage_subadmins INTEGER DEFAULT 0,
                can_manage_bot_profile INTEGER DEFAULT 0,
                can_test_sequence INTEGER DEFAULT 0,
                can_approve_requests INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS sequence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position INTEGER UNIQUE NOT NULL,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,  -- source channel ID
                has_buttons INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS seq_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_id INTEGER NOT NULL,
                row_num INTEGER NOT NULL,
                col_num INTEGER NOT NULL,
                button_text TEXT NOT NULL,
                button_url TEXT NOT NULL,
                color TEXT DEFAULT '⚪',
                FOREIGN KEY (sequence_id) REFERENCES sequence(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pending_requests (
                user_id INTEGER,
                chat_id INTEGER,
                created_at TIMESTAMP DEFAULT (DATETIME('now')),
                PRIMARY KEY (user_id, chat_id)
            );
        """)
        # Default configs
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('source_chat_id', ?)", (str(SOURCE_CHAT_ID),))
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('auto_approve', '0')")
    logger.info("Database ready.")

# ---------- DB helpers ----------
def db_upsert_user(user_id: int) -> bool:
    with get_conn() as c:
        return c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)).rowcount > 0

def db_total_users():
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def db_users_on_date(date_str: str):
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE first_seen = ?", (date_str,)).fetchone()[0]

def db_users_week():
    today = date.today()
    start = today - timedelta(days=today.weekday())
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE first_seen >= ? AND first_seen <= ?",
                         (start.isoformat(), today.isoformat())).fetchone()[0]

def db_users_month():
    today = date.today()
    start = today.replace(day=1)
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE first_seen >= ? AND first_seen <= ?",
                         (start.isoformat(), today.isoformat())).fetchone()[0]

def db_all_user_ids():
    with get_conn() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM users").fetchall()]

def db_get_auto_approve():
    with get_conn() as c:
        row = c.execute("SELECT value FROM config WHERE key='auto_approve'").fetchone()
        return row and row["value"] == "1"

def db_set_auto_approve(val: bool):
    with get_conn() as c:
        c.execute("UPDATE config SET value=? WHERE key='auto_approve'", ("1" if val else "0",))

def db_get_source_chat_id():
    with get_conn() as c:
        row = c.execute("SELECT value FROM config WHERE key='source_chat_id'").fetchone()
        return int(row["value"]) if row else SOURCE_CHAT_ID

def db_set_source_chat_id(chat_id: int):
    with get_conn() as c:
        c.execute("UPDATE config SET value=? WHERE key='source_chat_id'", (str(chat_id),))

# Sequence helpers
def db_add_sequence(msg_id: int, chat_id: int, position: int) -> int:
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO sequence (position, message_id, chat_id) VALUES (?,?,?)",
                  (position, msg_id, chat_id))
        return c.lastrowid

def db_get_sequence():
    with get_conn() as c:
        return c.execute("SELECT * FROM sequence ORDER BY position").fetchall()

def db_remove_sequence(position: int):
    with get_conn() as c:
        c.execute("DELETE FROM sequence WHERE position=?", (position,))

def db_reorder_sequence(pos1: int, pos2: int):
    with get_conn() as c:
        c.execute("UPDATE sequence SET position=-1 WHERE position=?", (pos2,))
        c.execute("UPDATE sequence SET position=? WHERE position=?", (pos2, pos1))
        c.execute("UPDATE sequence SET position=? WHERE position=-1", (pos1,))

def db_add_button(seq_id: int, row_num: int, col_num: int, text: str, url: str, color: str):
    with get_conn() as c:
        c.execute("INSERT INTO seq_buttons (sequence_id, row_num, col_num, button_text, button_url, color) VALUES (?,?,?,?,?,?)",
                  (seq_id, row_num, col_num, text, url, color))
        c.execute("UPDATE sequence SET has_buttons=1 WHERE id=?", (seq_id,))

def db_get_buttons(seq_id: int):
    with get_conn() as c:
        return c.execute("SELECT * FROM seq_buttons WHERE sequence_id=? ORDER BY row_num, col_num", (seq_id,)).fetchall()

def db_clear_buttons(seq_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM seq_buttons WHERE sequence_id=?", (seq_id,))
        c.execute("UPDATE sequence SET has_buttons=0 WHERE id=?", (seq_id,))

def db_delete_button(button_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM seq_buttons WHERE id=?", (button_id,))

# Pending requests
def db_add_pending(user_id: int, chat_id: int):
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO pending_requests (user_id, chat_id) VALUES (?,?)", (user_id, chat_id))

def db_get_pending():
    with get_conn() as c:
        return c.execute("SELECT * FROM pending_requests").fetchall()

def db_clear_pending():
    with get_conn() as c:
        c.execute("DELETE FROM pending_requests")

# Subadmin helpers
def db_is_subadmin(user_id: int):
    with get_conn() as c:
        return c.execute("SELECT 1 FROM subadmins WHERE user_id=?", (user_id,)).fetchone() is not None

def db_get_role(user_id: int):
    with get_conn() as c:
        row = c.execute("SELECT role FROM subadmins WHERE user_id=?", (user_id,)).fetchone()
        return row["role"] if row else None

def db_add_admin(user_id: int, role="subadmin"):
    with get_conn() as c:
        try:
            c.execute("INSERT INTO subadmins (user_id,role) VALUES (?,?)", (user_id, role))
            c.execute("INSERT INTO subadmin_perms (user_id) VALUES (?)", (user_id,))
            return True
        except sqlite3.IntegrityError:
            return False

def db_remove_admin(user_id: int):
    with get_conn() as c:
        return c.execute("DELETE FROM subadmins WHERE user_id=?", (user_id,)).rowcount > 0

def db_get_perms(user_id: int):
    with get_conn() as c:
        row = c.execute("SELECT * FROM subadmin_perms WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else {}

def db_set_perm(user_id: int, perm: str, val: bool):
    with get_conn() as c:
        c.execute(f"UPDATE subadmin_perms SET {perm}=? WHERE user_id=?", (int(val), user_id))

def is_main_admin(uid): return uid == ADMIN_ID

def db_has_perm(uid, perm):
    if is_main_admin(uid): return True
    perms = db_get_perms(uid)
    return perms.get(perm, False)

# ============ UTILS ============
async def run_sync(func, *args):
    return await asyncio.get_event_loop().run_in_executor(None, partial(func, *args))

# ============ KEYBOARDS ============
def admin_panel_kb():
    auto = "ON ✅" if db_get_auto_approve() else "OFF ❌"
    return ReplyKeyboardMarkup([
        ["📢 Broadcast", "📊 Stats"],
        ["📨 Sequence", "✅ Approve All"],
        ["🔄 Auto‑Approve: " + auto],
        ["📡 Change Source", "👥 Admins"],
        ["🤖 Bot Profile"],
    ], resize_keyboard=True)

def sequence_kb():
    return ReplyKeyboardMarkup([
        ["➕ Add Post", "📋 List Posts"],
        ["🔀 Reorder", "➖ Remove Post"],
        ["🔙 Back to Panel"],
    ], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)

# ============ PANEL HANDLERS ============
async def open_panel(update: Update, uid: int, note=""):
    await run_sync(db_upsert_user, uid)
    if not is_main_admin(uid) and not db_is_subadmin(uid):
        await update.message.reply_text("👋 Hello! I'm a content bot.")
        return
    if note:
        await update.message.reply_text(note, reply_markup=admin_panel_kb())
    else:
        await update.message.reply_text("🛠 Admin Panel", reply_markup=admin_panel_kb())

async def cmd_start(update, context):
    uid = update.effective_user.id
    await open_panel(update, uid)

# ============ SEQUENCE BUILDER (coloured buttons) ============
# We'll use a simple state machine: storing state in context.user_data
EDITING_SEQ = {}

async def seq_add_start(update, context):
    await update.message.reply_text("🔢 *Send the position number* (1,2,3…):", parse_mode='Markdown', reply_markup=cancel_kb())
    return "SEQ_POS"

async def seq_pos_sent(update, context):
    try:
        pos = int(update.message.text)
        if pos < 1: raise ValueError
    except:
        await update.message.reply_text("❌ Invalid number. Try again.", reply_markup=cancel_kb())
        return "SEQ_POS"
    context.user_data["seq_pos"] = pos
    await update.message.reply_text("📤 *Now send the post content* (text, photo, video, document, voice):", parse_mode='Markdown', reply_markup=cancel_kb())
    return "SEQ_CONTENT"

async def seq_content_received(update, context):
    msg = update.message
    user_data = context.user_data
    pos = user_data["seq_pos"]

    # Try to forward to source channel to obtain a message_id
    source = db_get_source_chat_id()
    try:
        forwarded = await msg.forward(chat_id=source)
        msg_id = forwarded.message_id
        chat_id = source
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to forward to source channel: {e}", reply_markup=admin_panel_kb())
        return ConversationHandler.END

    seq_id = db_add_sequence(msg_id, chat_id, pos)
    user_data["seq_id"] = seq_id

    # Ask for buttons
    keyboard = [["✅ Add Buttons", "⏭ Skip"]]
    await update.message.reply_text(
        "📌 *Post saved!* Do you want to add inline buttons?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
        parse_mode='Markdown'
    )
    return "SEQ_BUTTON_CHOICE"

async def seq_button_choice(update, context):
    choice = update.message.text
    if "Skip" in choice:
        await update.message.reply_text("✅ Post saved without buttons.", reply_markup=sequence_kb())
        return ConversationHandler.END
    # Start button wizard
    context.user_data["buttons"] = []  # list of rows, each row list of buttons
    context.user_data["current_row"] = []
    await update.message.reply_text("📝 *Send button name* (e.g. 'Join Channel'):", parse_mode='Markdown', reply_markup=cancel_kb())
    return "BTN_NAME"

async def btn_name(update, context):
    context.user_data["btn_name"] = update.message.text
    await update.message.reply_text("🔗 *Send button URL* (https://...):", parse_mode='Markdown', reply_markup=cancel_kb())
    return "BTN_URL"

async def btn_url(update, context):
    url = update.message.text
    if not url.startswith(("http://", "https://", "t.me/")):
        await update.message.reply_text("❌ URL must start with http/https/t.me. Try again:", reply_markup=cancel_kb())
        return "BTN_URL"
    context.user_data["btn_url"] = url
    # Colour picker
    kb = [["🟢 Green", "🔴 Red"], ["🔵 Blue", "⚪ Simple"]]
    await update.message.reply_text("🎨 *Pick button colour:*", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True), parse_mode='Markdown')
    return "BTN_COLOR"

async def btn_color(update, context):
    color = update.message.text.split()[0]  # emoji
    context.user_data["btn_color"] = color
    # Row placement
    kb = [["➕ Same Row", "↩️ New Row"]]
    await update.message.reply_text("📐 *Place on same row or start new?*", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True), parse_mode='Markdown')
    return "BTN_ROW"

async def btn_row(update, context):
    choice = update.message.text
    if "New" in choice:
        # save current row if any
        if context.user_data.get("current_row"):
            context.user_data["buttons"].append(context.user_data["current_row"])
            context.user_data["current_row"] = []
    # add button to current row
    btn = {
        "text": context.user_data["btn_name"],
        "url": context.user_data["btn_url"],
        "color": context.user_data["btn_color"]
    }
    context.user_data.setdefault("current_row", []).append(btn)

    total = sum(len(r) for r in context.user_data.get("buttons", [])) + len(context.user_data["current_row"])
    kb = [["➕ Add Another Button", "✅ Finish"], ["❌ Cancel"]]
    await update.message.reply_text(
        f"✅ *Button added!* (total {total})\nWhat next?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        parse_mode='Markdown'
    )
    return "BTN_MORE"

async def btn_more(update, context):
    action = update.message.text
    if "Add Another" in action:
        await update.message.reply_text("📝 *Send button name:*", parse_mode='Markdown', reply_markup=cancel_kb())
        return "BTN_NAME"
    if "Finish" in action:
        # Save all buttons to DB
        user_data = context.user_data
        seq_id = user_data["seq_id"]
        # Clear old buttons
        db_clear_buttons(seq_id)
        # Save rows
        all_rows = user_data.get("buttons", []).copy()
        if user_data.get("current_row"):
            all_rows.append(user_data["current_row"])
        for r_idx, row in enumerate(all_rows):
            for c_idx, btn in enumerate(row):
                db_add_button(seq_id, r_idx+1, c_idx+1, btn["text"], btn["url"], btn["color"])
        await update.message.reply_text("✅ *Post with buttons saved!*", reply_markup=sequence_kb(), parse_mode='Markdown')
        return ConversationHandler.END
    else:
        # Cancel
        await update.message.reply_text("❌ Cancelled.", reply_markup=sequence_kb())
        return ConversationHandler.END

async def cancel_seq(update, context):
    await update.message.reply_text("❌ Cancelled.", reply_markup=sequence_kb())
    return ConversationHandler.END

# ============ SEND SEQUENCE WITH BUTTONS ============
async def send_sequence_to_user(bot, user_id):
    seq = db_get_sequence()
    for item in seq:
        try:
            # Copy original message from source
            await bot.copy_message(chat_id=user_id, from_chat_id=item["chat_id"], message_id=item["message_id"])
            if item["has_buttons"]:
                buttons_data = db_get_buttons(item["id"])
                rows = {}
                for btn in buttons_data:
                    rows.setdefault(btn["row_num"], []).append(btn)
                keyboard = []
                for r in sorted(rows.keys()):
                    row_btns = []
                    for b in rows[r]:
                        label = f"{b['color']} {b['button_text']}"
                        row_btns.append(InlineKeyboardButton(label, url=b["button_url"]))
                    keyboard.append(row_btns)
                await bot.send_message(chat_id=user_id, text="🔘", reply_markup=InlineKeyboardMarkup(keyboard))
        except Forbidden:
            logger.info("User %s blocked bot", user_id)
            break
        except Exception as e:
            logger.error("Seq error: %s", e)
        await asyncio.sleep(BROADCAST_DELAY)

async def test_sequence(update, context):
    uid = update.effective_user.id
    if not db_has_perm(uid, "can_test_sequence"):
        await update.message.reply_text("⛔ No permission.")
        return
    await update.message.reply_text("🧪 Sending test sequence to you…")
    await send_sequence_to_user(context.bot, uid)
    await update.message.reply_text("✅ Done.", reply_markup=admin_panel_kb())

# ============ BROADCAST ============
async def broadcast_sequence(update, context):
    uid = update.effective_user.id
    if not db_has_perm(uid, "can_broadcast"):
        await update.message.reply_text("⛔ No permission.")
        return
    users = db_all_user_ids()
    status = await update.message.reply_text(f"📤 Broadcasting to {len(users)} users…")
    for u in users:
        await send_sequence_to_user(context.bot, u)
    await status.edit_text("✅ Broadcast completed.")

# ============ JOIN REQUEST ============
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr = update.chat_join_request
    uid = jr.from_user.id
    db_upsert_user(uid)
    await send_sequence_to_user(context.bot, uid)
    if db_get_auto_approve():
        try:
            await context.bot.approve_chat_join_request(chat_id=jr.chat.id, user_id=uid)
            logger.info("Auto-approved %s", uid)
        except Exception as e:
            logger.error("Auto-approve fail: %s", e)
    else:
        db_add_pending(uid, jr.chat.id)
        logger.info("Pending request stored for %s", uid)

async def approve_all(update, context):
    uid = update.effective_user.id
    if not db_has_perm(uid, "can_approve_requests"):
        await update.message.reply_text("⛔ No permission.")
        return
    pending = db_get_pending()
    if not pending:
        await update.message.reply_text("ℹ️ No pending requests.")
        return
    for req in pending:
        try:
            await context.bot.approve_chat_join_request(chat_id=req["chat_id"], user_id=req["user_id"])
        except Exception as e:
            logger.error("Approve fail: %s", e)
    db_clear_pending()
    await update.message.reply_text("✅ All requests approved.")

# ============ STATS ============
async def show_stats(update, context):
    uid = update.effective_user.id
    if not db_has_perm(uid, "can_stats"):
        await update.message.reply_text("⛔ No permission.")
        return

    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    total = db_total_users()
    today_new = db_users_on_date(today.isoformat())
    yesterday_new = db_users_on_date(yesterday.isoformat())
    # approximate tomorrow estimate based on average
    avg_daily = total / max((today - date(2024,1,1)).days, 1)
    tomorrow_est = round(avg_daily)
    week_new = db_users_week()
    month_new = db_users_month()
    pending = len(db_get_pending())
    auto = "ON ✅" if db_get_auto_approve() else "OFF ❌"

    text = (
        f"📊 **Stats**\n"
        f"👥 Total users: `{total}`\n"
        f"🗓 Today: `{today_new}` new\n"
        f"↩️ Yesterday: `{yesterday_new}`\n"
        f"📅 Tomorrow (est.): `{tomorrow_est}`\n"
        f"📆 This week: `{week_new}`\n"
        f"📆 This month: `{month_new}`\n"
        f"⏳ Pending: `{pending}`\n"
        f"🔄 Auto-approve: `{auto}`"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def toggle_auto(update, context):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("⛔ Only superadmin.")
        return
    current = db_get_auto_approve()
    db_set_auto_approve(not current)
    await update.message.reply_text(f"✅ Auto-approve now {'ON' if not current else 'OFF'}", reply_markup=admin_panel_kb())

# ============ TEXT HANDLER (panel buttons) ============
async def handle_panel_text(update, context):
    msg = update.message
    uid = update.effective_user.id
    text = msg.text or ""

    if not is_main_admin(uid) and not db_is_subadmin(uid):
        return

    if text == "📊 Stats":
        await show_stats(update, context)
    elif text == "📢 Broadcast":
        await broadcast_sequence(update, context)
    elif text == "✅ Approve All":
        await approve_all(update, context)
    elif text.startswith("🔄 Auto‑Approve:"):
        await toggle_auto(update, context)
    elif text == "📨 Sequence":
        await update.message.reply_text("🛠 Sequence Menu", reply_markup=sequence_kb())
    elif text == "📡 Change Source":
        await update.message.reply_text("Send new source chat ID:", reply_markup=cancel_kb())
        context.user_data["expect_source"] = True
    elif text == "👥 Admins":
        await update.message.reply_text("Admins management not fully implemented here.")
    elif text == "🤖 Bot Profile":
        await update.message.reply_text("Bot profile not fully implemented.")
    elif text == "🔙 Back to Panel":
        await update.message.reply_text("🛠 Panel", reply_markup=admin_panel_kb())
    # Sequence sub-menu handlers
    elif text == "➕ Add Post":
        from telegram.ext import ConversationHandler
        # We'll start a conversation
        return await seq_add_start(update, context)
    elif text == "📋 List Posts":
        seq = db_get_sequence()
        if not seq:
            await update.message.reply_text("Empty sequence.")
        else:
            txt = "\n".join(f"`{s['position']}` - msg_id `{s['message_id']}` (buttons: {s['has_buttons']})" for s in seq)
            await update.message.reply_text(txt, parse_mode='Markdown')
    elif text == "➖ Remove Post":
        await update.message.reply_text("Send position to remove:", reply_markup=cancel_kb())
        context.user_data["expect_remove"] = True
    elif text == "🔀 Reorder":
        await update.message.reply_text("Send `old_pos new_pos` (e.g., 2 1):", reply_markup=cancel_kb())
        context.user_data["expect_reorder"] = True
    elif context.user_data.get("expect_source"):
        try:
            new_id = int(text)
            db_set_source_chat_id(new_id)
            await update.message.reply_text(f"✅ Source set to `{new_id}`", reply_markup=admin_panel_kb())
        except ValueError:
            await update.message.reply_text("❌ Invalid ID.")
        finally:
            context.user_data.pop("expect_source", None)
    elif context.user_data.get("expect_remove"):
        try:
            pos = int(text)
            db_remove_sequence(pos)
            await update.message.reply_text("✅ Removed.", reply_markup=sequence_kb())
        except ValueError:
            await update.message.reply_text("❌ Invalid number.")
        finally:
            context.user_data.pop("expect_remove", None)
    elif context.user_data.get("expect_reorder"):
        parts = text.split()
        if len(parts) == 2:
            try:
                p1, p2 = map(int, parts)
                db_reorder_sequence(p1, p2)
                await update.message.reply_text("✅ Reordered.", reply_markup=sequence_kb())
            except:
                await update.message.reply_text("❌ Error.")
        else:
            await update.message.reply_text("❌ Send two numbers.")
        context.user_data.pop("expect_reorder", None)

# ============ CONVERSATION HANDLER FOR ADD POST ============
from telegram.ext import ConversationHandler
SEQ_CONV = ConversationHandler(
    entry_points=[MessageHandler(filters.Text(["➕ Add Post"]), seq_add_start)],
    states={
        "SEQ_POS": [MessageHandler(filters.TEXT & ~filters.COMMAND, seq_pos_sent)],
        "SEQ_CONTENT": [MessageHandler(filters.ALL & ~filters.COMMAND, seq_content_received)],
        "SEQ_BUTTON_CHOICE": [MessageHandler(filters.TEXT & ~filters.COMMAND, seq_button_choice)],
        "BTN_NAME": [MessageHandler(filters.TEXT & ~filters.COMMAND, btn_name)],
        "BTN_URL": [MessageHandler(filters.TEXT & ~filters.COMMAND, btn_url)],
        "BTN_COLOR": [MessageHandler(filters.TEXT & ~filters.COMMAND, btn_color)],
        "BTN_ROW": [MessageHandler(filters.TEXT & ~filters.COMMAND, btn_row)],
        "BTN_MORE": [MessageHandler(filters.TEXT & ~filters.COMMAND, btn_more)],
    },
    fallbacks=[MessageHandler(filters.Text("❌ Cancel"), cancel_seq)],
    allow_reentry=True,
)

# ============ MAIN ============
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(SEQ_CONV)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_panel_text))

    logger.info("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()