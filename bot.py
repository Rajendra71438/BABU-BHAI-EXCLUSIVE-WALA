import os, logging, sqlite3, requests, json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatJoinRequestHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
load_dotenv()
BOT_TOKEN      = os.getenv("BOT_TOKEN")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "0"))
SOURCE_CHAT_ID = os.getenv("SOURCE_CHAT_ID", "")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = "bot.db"

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sequence (
            position      INTEGER PRIMARY KEY,
            content_type  TEXT    NOT NULL,
            content       TEXT    NOT NULL,
            caption       TEXT    DEFAULT '',
            source_msg_id INTEGER DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            first_seen DATE DEFAULT (DATE('now'))
        );
        CREATE TABLE IF NOT EXISTS pending_requests (
            user_id    INTEGER,
            chat_id    INTEGER,
            created_at TIMESTAMP DEFAULT (DATETIME('now')),
            PRIMARY KEY (user_id, chat_id)
        );

        INSERT OR IGNORE INTO config (key, value) VALUES ('auto_approve',           '0');
        INSERT OR IGNORE INTO config (key, value) VALUES ('send_on_start',          '1');
        INSERT OR IGNORE INTO config (key, value) VALUES ('source_chat_id_runtime', '');
    """)
    conn.commit()
    conn.close()

    # 🔥 AUTO-DELETE the default welcome message if it exists
    _remove_default_welcome()

def _remove_default_welcome():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT position FROM sequence WHERE position = 1 AND content = '👋 Welcome! This is the default welcome post.'"
    ).fetchone()
    if row:
        conn.execute("DELETE FROM sequence WHERE position = 1")
        print("🗑️ Removed default welcome message from database.")
    conn.commit()
    conn.close()

def db_get_config(key):
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else ""

def db_set_config(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (key, value))
    conn.commit(); conn.close()

def get_source():
    return SOURCE_CHAT_ID or db_get_config("source_chat_id_runtime")

def db_get_sequence():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT position, content_type, content, caption, source_msg_id "
        "FROM sequence ORDER BY position"
    ).fetchall()
    conn.close()
    return rows

def db_get_seq_entry(pos):
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        "SELECT position, content_type, content, caption, source_msg_id "
        "FROM sequence WHERE position=?", (pos,)
    ).fetchone()
    conn.close()
    return row

def db_seq_count():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM sequence").fetchone()[0]
    conn.close()
    return n

def db_welcome_exists():
    return db_get_seq_entry(1) is not None

def db_set_welcome(ctype, content, caption, src_msg_id=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO sequence (position, content_type, content, caption, source_msg_id) "
        "VALUES (1,?,?,?,?) "
        "ON CONFLICT(position) DO UPDATE SET "
        "content_type=excluded.content_type, content=excluded.content, "
        "caption=excluded.caption, source_msg_id=excluded.source_msg_id",
        (ctype, content, caption, src_msg_id)
    )
    conn.commit(); conn.close()

def db_insert_seq(pos, ctype, content, caption, src_msg_id=None):
    """Insert new message at position pos, shifting higher positions up."""
    conn = sqlite3.connect(DB_PATH)
    # Get current max position
    max_pos = conn.execute("SELECT MAX(position) FROM sequence").fetchone()[0]
    if max_pos is not None:
        # Shift from highest down to pos
        for p in range(max_pos, pos - 1, -1):
            conn.execute("UPDATE sequence SET position = ? WHERE position = ?", (p + 1, p))
    conn.execute(
        "INSERT INTO sequence (position, content_type, content, caption, source_msg_id) VALUES (?,?,?,?,?)",
        (pos, ctype, content, caption, src_msg_id)
    )
    conn.commit()
    conn.close()

def db_remove_seq(pos):
    """Delete message at position pos and shift higher positions down."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM sequence WHERE position = ?", (pos,))
    conn.execute("UPDATE sequence SET position = position - 1 WHERE position > ?", (pos,))
    conn.commit()
    conn.close()

def db_update_source_msg_id(pos, msg_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE sequence SET source_msg_id=? WHERE position=?", (msg_id, pos))
    conn.commit(); conn.close()

def db_upsert_user(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (uid,))
    conn.commit(); conn.close()

def db_total_users():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n

def db_users_since(days):
    conn  = sqlite3.connect(DB_PATH)
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    n = conn.execute("SELECT COUNT(*) FROM users WHERE first_seen>=?", (since,)).fetchone()[0]
    conn.close()
    return n

def db_add_pending(uid, cid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO pending_requests(user_id,chat_id) VALUES (?,?)", (uid, cid))
    conn.commit(); conn.close()

def db_get_pending():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM pending_requests").fetchall()
    conn.close()
    return rows

def db_clear_pending():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pending_requests")
    conn.commit(); conn.close()

def is_admin(uid):
    return uid == ADMIN_ID


# ══════════════════════════════════════════════
#  FIXED COLORED BUTTONS (only for position 1)
# ══════════════════════════════════════════════
FIXED_BUTTONS = [
    {"text": "➤ 𝐂𝐎𝐓𝐓𝐎𝐍 𝐂𝐀𝐍𝐃𝐘",  "url": "https://t.me/+W6-au4_teS83NDIx",  "style": "danger"},
    {"text": "➤ 𝐁𝐀𝐒𝐈𝐂 𝐍𝐄𝐄𝐃",      "url": "https://t.me/+3WSjyKn7ZP9iYWY1", "style": "primary"},
    {"text": "➤ 𝐓𝐇𝐄 𝐓𝐄𝐀𝐒𝐄 𝐑𝐎𝐎𝐌", "url": "https://t.me/The_Teaser_room",    "style": "success"},
]

def _inline_kb():
    rows = [[{"text": b["text"], "url": b["url"], "style": b["style"]}] for b in FIXED_BUTTONS]
    return json.dumps({"inline_keyboard": rows})


# ══════════════════════════════════════════════
#  TELEGRAM HTTP HELPERS
# ══════════════════════════════════════════════
def _post(endpoint, payload):
    try:
        r = requests.post(f"{API_URL}/{endpoint}", json=payload, timeout=20)
        return r.json()
    except Exception as e:
        logger.error("HTTP %s: %s", endpoint, e)
        return None

def _send_direct(chat_id: int, entry: tuple, with_buttons: bool) -> dict | None:
    _, ctype, content, caption, _ = entry
    kb = _inline_kb() if with_buttons else None

    if ctype == "text":
        payload = {"chat_id": chat_id, "text": content, "parse_mode": "HTML"}
        if kb:
            payload["reply_markup"] = kb
        return _post("sendMessage", payload)

    method = {
        "photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio",
        "voice": "sendVoice", "document": "sendDocument"
    }.get(ctype, "sendDocument")
    payload = {"chat_id": chat_id, ctype: content, "caption": caption, "parse_mode": "HTML"}
    if kb:
        payload["reply_markup"] = kb
    return _post(method, payload)

def _copy_from_source(chat_id: int, src_msg_id: int, with_buttons: bool) -> dict | None:
    src = get_source()
    if not src:
        return None
    payload = {"chat_id": chat_id, "from_chat_id": int(src), "message_id": src_msg_id}
    if with_buttons:
        payload["reply_markup"] = _inline_kb()
    return _post("copyMessage", payload)

def send_entry_to_user(chat_id: int, entry: tuple):
    pos, ctype, content, caption, src_msg_id = entry
    with_buttons = (pos == 1)

    if src_msg_id:
        result = _copy_from_source(chat_id, src_msg_id, with_buttons)
        if result and result.get("ok"):
            return result
    return _send_direct(chat_id, entry, with_buttons)

def forward_to_source(pos: int, entry: tuple = None) -> int | None:
    src = get_source()
    if not src:
        logger.error("No source channel configured")
        return None

    if entry is None:
        entry = db_get_seq_entry(pos)
        if not entry:
            return None

    result = _send_direct(int(src), entry, with_buttons=False)
    if result and result.get("ok"):
        msg_id = result["result"]["message_id"]
        db_update_source_msg_id(pos, msg_id)
        return msg_id

    logger.error("Failed to forward to source: %s", result)
    return None

def deliver_sequence(chat_id: int):
    for entry in db_get_sequence():
        send_entry_to_user(chat_id, entry)


# ══════════════════════════════════════════════
#  MEDIA EXTRACTOR
# ══════════════════════════════════════════════
def extract_media(msg):
    if msg.text:     return "text",     msg.text,              ""
    if msg.photo:    return "photo",    msg.photo[-1].file_id, msg.caption or ""
    if msg.video:    return "video",    msg.video.file_id,     msg.caption or ""
    if msg.audio:    return "audio",    msg.audio.file_id,     msg.caption or ""
    if msg.voice:    return "voice",    msg.voice.file_id,     msg.caption or ""
    if msg.document: return "document", msg.document.file_id,  msg.caption or ""
    return "", "", ""


# ══════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════
def admin_main_kb():
    auto     = db_get_config("auto_approve") == "1"
    auto_lbl = "🔄 AUTO-APPROVE: " + ("ON ✅" if auto else "OFF ❌")
    send_on  = db_get_config("send_on_start") == "1"
    send_lbl = "📨 SEND ON START: " + ("ON ✅" if send_on else "OFF ❌")
    return ReplyKeyboardMarkup([
        ["📊 STATS",          "📡 CHANGE SOURCE"],
        ["✅ APPROVE ALL",    auto_lbl],
        [send_lbl],
        ["📋 MSG SEQUENCE",   "🧪 TEST SEQUENCE"],
    ], resize_keyboard=True)

def stats_kb():
    return ReplyKeyboardMarkup([
        ["📅 TODAY",   "📅 TOMORROW"],
        ["📆 WEEKLY",  "📊 TOTAL"],
        ["🔙 BACK TO PANEL"],
    ], resize_keyboard=True)

def sequence_kb():
    return ReplyKeyboardMarkup([
        ["✏️ SET WELCOME MSG"],
        ["➕ ADD MSG",    "➖ REMOVE MSG"],
        ["🔙 BACK TO PANEL"],
    ], resize_keyboard=True)


# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db_upsert_user(uid)

    if is_admin(uid):
        context.user_data.clear()
        await update.message.reply_text("🛠 ADMIN PANEL", reply_markup=admin_main_kb())
        return

    if db_get_config("send_on_start") == "1" and db_welcome_exists():
        deliver_sequence(uid)


# ══════════════════════════════════════════════
#  JOIN REQUEST
# ══════════════════════════════════════════════
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr  = update.chat_join_request
    uid = jr.from_user.id
    cid = jr.chat.id
    db_upsert_user(uid)

    if db_welcome_exists():
        deliver_sequence(uid)

    if db_get_config("auto_approve") == "1":
        try:
            await context.bot.approve_chat_join_request(chat_id=cid, user_id=uid)
        except TelegramError as e:
            logger.error("Auto-approve fail: %s", e)
    else:
        db_add_pending(uid, cid)


# ══════════════════════════════════════════════
#  ADMIN HANDLER
# ══════════════════════════════════════════════
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    msg = update.message
    if not msg:
        return

    text = (msg.text or "").strip()
    ud   = context.user_data

    # ── BACK ──────────────────────────────────────────────────────────
    if text == "🔙 BACK TO PANEL":
        ud.clear()
        await msg.reply_text("🛠 ADMIN PANEL", reply_markup=admin_main_kb())
        return

    # ── STATS ──────────────────────────────────────────────────────────
    if text == "📊 STATS":
        ud.clear()
        await msg.reply_text("📊 CHOOSE STATS VIEW:", reply_markup=stats_kb())
        return

    if text == "📅 TODAY":
        await msg.reply_text(f"📅 NEW USERS TODAY: {db_users_since(0)}", reply_markup=stats_kb())
        return

    if text == "📅 TOMORROW":
        await msg.reply_text(
            f"ℹ️ TOMORROW PROJECTION (BASED ON TODAY):\n📅 TODAY: {db_users_since(0)} NEW USERS",
            reply_markup=stats_kb()
        )
        return

    if text == "📆 WEEKLY":
        await msg.reply_text(f"📆 NEW USERS THIS WEEK: {db_users_since(7)}", reply_markup=stats_kb())
        return

    if text == "📊 TOTAL":
        await msg.reply_text(
            f"📊 FULL STATS\n\n"
            f"👥 TOTAL USERS: {db_total_users()}\n"
            f"📅 TODAY: {db_users_since(0)}\n"
            f"📆 THIS WEEK: {db_users_since(7)}\n"
            f"🗓 THIS MONTH: {db_users_since(30)}\n"
            f"⏳ PENDING REQUESTS: {len(db_get_pending())}\n"
            f"🔄 AUTO-APPROVE: {'ON' if db_get_config('auto_approve')=='1' else 'OFF'}\n"
            f"📨 SEND ON START: {'ON' if db_get_config('send_on_start')=='1' else 'OFF'}\n"
            f"📋 SEQUENCE MSGS: {db_seq_count()}",
            reply_markup=stats_kb()
        )
        return

    # ── TOGGLES / APPROVE ─────────────────────────────────────────────
    if text == "✅ APPROVE ALL":
        pending = db_get_pending()
        if not pending:
            await msg.reply_text("ℹ️ NO PENDING REQUESTS.", reply_markup=admin_main_kb())
            return
        for req in pending:
            try:
                await context.bot.approve_chat_join_request(chat_id=req[1], user_id=req[0])
            except TelegramError as e:
                logger.error("Approve fail: %s", e)
        db_clear_pending()
        await msg.reply_text("✅ ALL REQUESTS APPROVED.", reply_markup=admin_main_kb())
        return

    if text.startswith("🔄 AUTO-APPROVE:"):
        cur = db_get_config("auto_approve") == "1"
        db_set_config("auto_approve", "0" if cur else "1")
        await msg.reply_text(
            f"AUTO-APPROVE {'OFF ❌' if cur else 'ON ✅'}", reply_markup=admin_main_kb()
        )
        return

    if text.startswith("📨 SEND ON START:"):
        cur = db_get_config("send_on_start") == "1"
        db_set_config("send_on_start", "0" if cur else "1")
        await msg.reply_text(
            f"SEND ON START {'OFF ❌' if cur else 'ON ✅'}", reply_markup=admin_main_kb()
        )
        return

    if text == "📡 CHANGE SOURCE":
        src = get_source() or "NOT SET"
        await msg.reply_text(
            f"CURRENT SOURCE CHANNEL ID: `{src}`\n\n"
            f"SEND THE NEW NUMERIC CHANNEL ID (e.g. -1001234567890)\n"
            f"BOT MUST BE ADMIN IN THAT CHANNEL.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        ud["awaiting_source"] = True
        return

    # ── TEST SEQUENCE ─────────────────────────────────────────────────
    if text == "🧪 TEST SEQUENCE":
        if not db_welcome_exists():
            await msg.reply_text(
                "⚠️ NO WELCOME MSG SET YET. GO TO 📋 MSG SEQUENCE → ✏️ SET WELCOME MSG FIRST.",
                reply_markup=admin_main_kb()
            )
            return
        seq = db_get_sequence()
        await msg.reply_text(
            f"🧪 SENDING {len(seq)} MSG(S) TO YOU AS TEST...",
            reply_markup=ReplyKeyboardRemove()
        )
        for entry in seq:
            send_entry_to_user(uid, entry)
        await msg.reply_text("✅ TEST DONE.", reply_markup=admin_main_kb())
        return

    # ── SEQUENCE PANEL ────────────────────────────────────────────────
    if text == "📋 MSG SEQUENCE":
        ud.clear()
        seq   = db_get_sequence()
        lines = [f"📋 SEQUENCE ({len(seq)} MSG(S)):"]
        if not seq:
            lines.append("  ⚠️ EMPTY — SET WELCOME MSG FIRST.")
        for pos, ctype, content, caption, src_id in seq:
            tag     = "🔑 WELCOME (WITH BUTTONS)" if pos == 1 else f"MSG {pos} (NO BUTTONS)"
            preview = content[:38] + "…" if len(content) > 38 else content
            src_info = f" [src:{src_id}]" if src_id else " [NOT IN SOURCE]"
            lines.append(f"  [{tag}] {ctype.upper()}: {preview}{src_info}")
        await msg.reply_text("\n".join(lines), reply_markup=sequence_kb())
        return

    if text == "✏️ SET WELCOME MSG":
        await msg.reply_text(
            "📤 SEND THE WELCOME MESSAGE (POSITION 1).\n"
            "SUPPORTS: TEXT, PHOTO, VIDEO, AUDIO, VOICE, DOCUMENT + CAPTION.\n\n"
            "⚡ THIS MSG WILL HAVE THE 3 COLORED BUTTONS ATTACHED.\n"
            "IT WILL ALSO BE FORWARDED TO THE SOURCE CHANNEL.",
            reply_markup=ReplyKeyboardRemove()
        )
        ud["set_welcome"] = True
        return

    if text == "➕ ADD MSG":
        cnt = db_seq_count()
        if not db_welcome_exists():
            await msg.reply_text(
                "⚠️ SET THE WELCOME MSG FIRST BEFORE ADDING MORE MESSAGES.",
                reply_markup=sequence_kb()
            )
            return
        src = get_source()
        if not src:
            await msg.reply_text(
                "❌ NO SOURCE CHANNEL CONFIGURED.\n"
                "Please set it first using 📡 CHANGE SOURCE.",
                reply_markup=sequence_kb()
            )
            return
        await msg.reply_text(
            f"📍 SEQUENCE HAS {cnt} MSG(S).\n"
            f"ENTER POSITION FOR NEW MSG (2 TO {cnt + 1}):\n\n"
            f"NOTE: MSGS FROM POSITION 2 ONWARD HAVE NO BUTTONS.\n"
            f"After you send the content, I will forward it to the source channel and save its ID.",
            reply_markup=ReplyKeyboardRemove()
        )
        ud["add_step"] = "position"
        return

    if text == "➖ REMOVE MSG":
        seq = db_get_sequence()
        if not seq:
            await msg.reply_text(
                "⚠️ SEQUENCE IS EMPTY. NOTHING TO REMOVE.",
                reply_markup=sequence_kb()
            )
            return
        lines = ["📋 REMOVABLE MESSAGES (ALL POSITIONS):"]
        for pos, ctype, content, _, _ in seq:
            preview = content[:35] + "…" if len(content) > 35 else content
            lines.append(f"  [{pos}] {ctype.upper()}: {preview}")
        await msg.reply_text(
            "\n".join(lines) + "\n\nSEND THE POSITION NUMBER TO REMOVE:",
            reply_markup=ReplyKeyboardRemove()
        )
        ud["remove_step"] = "position"
        return

    # ── STATES (AWAITING INPUT) ───────────────────────────────────────

    if ud.get("awaiting_source"):
        try:
            new_id = int(text)
            test_payload = {"chat_id": new_id, "text": "✅ Source channel test – bot is working"}
            test_result = _post("sendMessage", test_payload)
            if test_result and test_result.get("ok"):
                db_set_config("source_chat_id_runtime", str(new_id))
                await msg.reply_text(
                    f"✅ SOURCE CHANNEL SET TO `{new_id}`\nTest message sent successfully.",
                    parse_mode="Markdown",
                    reply_markup=admin_main_kb()
                )
            else:
                await msg.reply_text(
                    f"❌ Bot cannot send to `{new_id}`.\n"
                    "Make sure the ID is correct and the bot is an admin (with post permissions) in that channel.",
                    parse_mode="Markdown",
                    reply_markup=admin_main_kb()
                )
        except ValueError:
            await msg.reply_text("❌ INVALID ID. SEND A NUMERIC CHAT ID.", reply_markup=admin_main_kb())
        ud.pop("awaiting_source", None)
        return

    if ud.get("set_welcome"):
        ctype, content, caption = extract_media(msg)
        if not content:
            await msg.reply_text("❌ UNSUPPORTED TYPE. SEND TEXT, PHOTO, VIDEO, AUDIO, VOICE, OR DOCUMENT.")
            return
        db_set_welcome(ctype, content, caption, src_msg_id=None)
        entry = db_get_seq_entry(1)
        msg_id = forward_to_source(1, entry)
        if msg_id:
            note = f"\n📡 FORWARDED TO SOURCE CHANNEL (MSG ID: {msg_id})."
        else:
            note = "\n⚠️ COULD NOT FORWARD — CHECK SOURCE CHANNEL ID/PERMISSIONS. The message is saved but will be sent directly to users."
        ud.pop("set_welcome", None)
        await msg.reply_text(
            f"✅ WELCOME MESSAGE SET.{note}\n\n"
            f"NOW YOU CAN ADD MORE MSGS VIA ➕ ADD MSG.",
            reply_markup=sequence_kb()
        )
        return

    if ud.get("add_step") == "position":
        cnt = db_seq_count()
        try:
            pos = int(text)
            if pos < 2 or pos > cnt + 1:
                raise ValueError
            ud["add_pos"]  = pos
            ud["add_step"] = "content"
            await msg.reply_text(
                f"📤 POSITION SET TO {pos}.\n\n"
                f"NOW SEND THE MESSAGE CONTENT\n"
                f"(TEXT, PHOTO, VIDEO, AUDIO, VOICE, DOCUMENT + OPTIONAL CAPTION).\n"
                f"NO BUTTONS WILL BE ATTACHED TO THIS MSG.\n"
                f"It will be forwarded to the source channel automatically."
            )
        except ValueError:
            await msg.reply_text(f"❌ INVALID. ENTER A NUMBER FROM 2 TO {cnt + 1}.")
        return

    if ud.get("add_step") == "content":
        pos = ud["add_pos"]
        ctype, content, caption = extract_media(msg)
        if not content:
            await msg.reply_text("❌ UNSUPPORTED TYPE. TRY AGAIN.")
            return
        db_insert_seq(pos, ctype, content, caption, src_msg_id=None)
        entry = db_get_seq_entry(pos)
        msg_id = forward_to_source(pos, entry)
        if msg_id:
            note = f"\n📡 FORWARDED TO SOURCE CHANNEL (MSG ID: {msg_id})."
        else:
            note = "\n⚠️ COULD NOT FORWARD — CHECK SOURCE CHANNEL ID/PERMISSIONS. The message is saved but will be sent directly to users."
        ud.clear()
        await msg.reply_text(
            f"✅ MESSAGE ADDED AT POSITION {pos}.{note}", reply_markup=sequence_kb()
        )
        return

    if ud.get("remove_step") == "position":
        cnt = db_seq_count()
        try:
            pos = int(text)
            if pos < 1 or pos > cnt:
                raise ValueError
            db_remove_seq(pos)
            ud.pop("remove_step", None)
            await msg.reply_text(
                f"✅ MSG AT POSITION {pos} REMOVED. SEQUENCE NOW HAS {db_seq_count()} MSG(S).",
                reply_markup=sequence_kb()
            )
        except ValueError:
            await msg.reply_text(f"❌ INVALID. ENTER A NUMBER FROM 1 TO {cnt}.")
        return


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_message))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL,
        handle_admin_message
    ))

    print("BOT IS RUNNING ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
