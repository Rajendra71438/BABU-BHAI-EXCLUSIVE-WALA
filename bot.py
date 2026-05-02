import os, logging, sqlite3, requests, json
from datetime import date
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatJoinRequestHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError

# ---------- CONFIG ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = "bot.db"

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ---------- DATABASE ----------
def init_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS post (
            id INTEGER PRIMARY KEY CHECK(id=1),
            content_type TEXT NOT NULL,
            content TEXT NOT NULL,
            caption TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen DATE DEFAULT (DATE('now'))
        );
        CREATE TABLE IF NOT EXISTS pending_requests (
            user_id INTEGER, chat_id INTEGER,
            created_at TIMESTAMP DEFAULT (DATETIME('now')),
            PRIMARY KEY (user_id, chat_id)
        );
        INSERT OR IGNORE INTO config (key, value) VALUES ('source_chat_id', '');
        INSERT OR IGNORE INTO config (key, value) VALUES ('auto_approve', '0');
        INSERT OR IGNORE INTO post (id, content_type, content, caption)
        VALUES (1, 'text', '👋 Welcome! This is the default post.', '');
    """)
    conn.commit()
    conn.close()

def db_get_post():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT content_type, content, caption FROM post WHERE id=1").fetchone()
    conn.close()
    return row

def db_set_post(content_type, content, caption):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE post SET content_type=?, content=?, caption=? WHERE id=1",
                 (content_type, content, caption))
    conn.commit()
    conn.close()

def db_get_config(key):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else ""

def db_set_config(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE config SET value=? WHERE key=?", (value, key))
    conn.commit()
    conn.close()

def db_upsert_user(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (uid,))
    conn.commit()
    conn.close()

def db_total_users():
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return count

def db_add_pending(uid, cid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO pending_requests (user_id, chat_id) VALUES (?,?)", (uid, cid))
    conn.commit()
    conn.close()

def db_get_pending():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM pending_requests").fetchall()
    conn.close()
    return rows

def db_clear_pending():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pending_requests")
    conn.commit()
    conn.close()

def is_admin(uid):
    return uid == ADMIN_ID

# ---------- FIXED COLORED BUTTONS (each on its own row) ----------
FIXED_BUTTONS = [
    {
        "text": "➤ 𝐂𝐎𝐓𝐓𝐎𝐍 𝐂𝐀𝐍𝐃𝐘",
        "url": "https://t.me/+W6-au4_teS83NDIx",
        "style": "danger"       # 🔴 red
    },
    {
        "text": "➤ 𝐁𝐀𝐒𝐈𝐂 𝐍𝐄𝐄𝐃",
        "url": "https://t.me/+3WSjyKn7ZP9iYWY1",
        "style": "primary"      # 🔵 blue
    },
    {
        "text": "➤ 𝐓𝐇𝐄 𝐓𝐄𝐀𝐒𝐄 𝐑𝐎𝐎𝐌",
        "url": "https://t.me/The_Teaser_room",
        "style": "success"      # 🟢 green
    }
]

def build_inline_keyboard(buttons):
    """Create an inline keyboard with each button on its own row"""
    rows = []
    for btn in buttons:
        rows.append([{
            "text": btn["text"],
            "url": btn["url"],
            "style": btn["style"]
        }])
    return json.dumps({"inline_keyboard": rows})

def send_post(chat_id, post_data):
    """Send the stored post + coloured buttons via API"""
    content_type, content, caption = post_data
    keyboard = build_inline_keyboard(FIXED_BUTTONS)

    if content_type == "text":
        url = f"{API_URL}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": content,
            "reply_markup": keyboard,
            "parse_mode": "HTML"
        }
    else:
        url = f"{API_URL}/send{content_type.capitalize()}"
        payload = {
            "chat_id": chat_id,
            content_type: content,
            "caption": caption,
            "reply_markup": keyboard,
            "parse_mode": "HTML"
        }

    try:
        resp = requests.post(url, json=payload)
        return resp.json()
    except Exception as e:
        logger.error("Send failed: %s", e)
        return None

# ---------- KEYBOARDS ----------
def admin_panel_kb():
    auto = db_get_config("auto_approve") == "1"
    label = "🔄 Auto‑Approve: " + ("ON ✅" if auto else "OFF ❌")
    return ReplyKeyboardMarkup([
        ["📝 Set Post", "📊 Stats"],
        ["✅ Approve All", label],
        ["📡 Change Source"]
    ], resize_keyboard=True)

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db_upsert_user(uid)

    if is_admin(uid):
        await update.message.reply_text("🛠 Admin Panel", reply_markup=admin_panel_kb())
        return

    # Send the current post (even the default)
    post = db_get_post()
    send_post(uid, post)

# ---------- JOIN REQUEST ----------
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr = update.chat_join_request
    uid = jr.from_user.id
    cid = jr.chat.id

    db_upsert_user(uid)
    post = db_get_post()
    send_post(uid, post)

    if db_get_config("auto_approve") == "1":
        try:
            await context.bot.approve_chat_join_request(chat_id=cid, user_id=uid)
        except TelegramError as e:
            logger.error("Auto-approve fail: %s", e)
    else:
        db_add_pending(uid, cid)

# ---------- ADMIN PANEL HANDLERS ----------
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    msg = update.message
    if msg is None:
        return

    text = msg.text.strip() if msg.text else ""

    # --- Button presses ---
    if text == "📝 Set Post":
        await update.message.reply_text(
            "📤 Send the new post (text, photo, video, audio, document, voice).\n"
            "It will be stored and sent to users who start the bot or request to join.",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["awaiting_post"] = True
        return

    if text == "📊 Stats":
        total = db_total_users()
        pending = len(db_get_pending())
        auto = "ON" if db_get_config("auto_approve") == "1" else "OFF"
        msg_text = f"👥 Total users: {total}\n⏳ Pending: {pending}\n🔄 Auto‑approve: {auto}"
        await update.message.reply_text(msg_text, reply_markup=admin_panel_kb())
        return

    if text == "✅ Approve All":
        pending = db_get_pending()
        if not pending:
            await update.message.reply_text("ℹ️ No pending requests.", reply_markup=admin_panel_kb())
            return
        for req in pending:
            try:
                await context.bot.approve_chat_join_request(chat_id=req[1], user_id=req[0])
            except TelegramError as e:
                logger.error("Approve fail: %s", e)
        db_clear_pending()
        await update.message.reply_text("✅ All requests approved.", reply_markup=admin_panel_kb())
        return

    if text.startswith("🔄 Auto‑Approve:"):
        current = db_get_config("auto_approve") == "1"
        db_set_config("auto_approve", "0" if current else "1")
        await update.message.reply_text(
            f"Auto‑approve {'OFF ❌' if current else 'ON ✅'}",
            reply_markup=admin_panel_kb()
        )
        return

    if text == "📡 Change Source":
        current = db_get_config("source_chat_id")
        await update.message.reply_text(
            f"Current source ID: `{current}`\nSend new numeric chat ID:",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["awaiting_source"] = True
        return

    # --- State: waiting for a new post ---
    if context.user_data.get("awaiting_post"):
        content_type = "text"
        content = ""
        caption = ""

        if msg.text:
            content = msg.text
        elif msg.photo:
            content_type = "photo"
            content = msg.photo[-1].file_id
            caption = msg.caption or ""
        elif msg.video:
            content_type = "video"
            content = msg.video.file_id
            caption = msg.caption or ""
        elif msg.audio:
            content_type = "audio"
            content = msg.audio.file_id
            caption = msg.caption or ""
        elif msg.voice:
            content_type = "voice"
            content = msg.voice.file_id
            caption = msg.caption or ""
        elif msg.document:
            content_type = "document"
            content = msg.document.file_id
            caption = msg.caption or ""
        else:
            await update.message.reply_text("❌ Unsupported type. Try again.")
            return

        db_set_post(content_type, content, caption)
        context.user_data.pop("awaiting_post", None)
        await update.message.reply_text("✅ Post updated!", reply_markup=admin_panel_kb())
        return

    # --- State: waiting for new source chat ID ---
    if context.user_data.get("awaiting_source"):
        try:
            new_id = int(text)
            db_set_config("source_chat_id", str(new_id))
            await update.message.reply_text(
                f"✅ Source set to `{new_id}`",
                parse_mode='Markdown',
                reply_markup=admin_panel_kb()
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid ID.", reply_markup=admin_panel_kb())
        finally:
            context.user_data.pop("awaiting_source", None)
        return

# ---------- MAIN ----------
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

    print("BOT IS RUNNING")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
