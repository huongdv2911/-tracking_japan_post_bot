import requests
import sqlite3
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ===== CONFIG =====
TOKEN = os.getenv("TOKEN")
CHECK_INTERVAL = 1800  # 30 phút

# ===== DATABASE =====
conn = sqlite3.connect("tracking.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS trackings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_number TEXT,
    chat_id INTEGER,
    last_status TEXT
)
""")
conn.commit()

# ===== AUTO MIGRATE =====
def column_exists(column_name):
    cursor.execute("PRAGMA table_info(trackings)")
    columns = [col[1] for col in cursor.fetchall()]
    return column_name in columns

if not column_exists("user_id"):
    cursor.execute("ALTER TABLE trackings ADD COLUMN user_id INTEGER")

if not column_exists("username"):
    cursor.execute("ALTER TABLE trackings ADD COLUMN username TEXT")

conn.commit()


# ===== CHECK JAPAN POST =====
def get_tracking_status(tracking_number):
    url = "https://trackings.post.japanpost.jp/services/srv/search/direct"

    payload = {
        "reqCodeNo1": tracking_number,
        "searchKind": "S002",
        "locale": "ja"
    }

    try:
        res = requests.post(url, data=payload, timeout=15)
        text = res.text

        if "該当なし" in text or "お問い合わせ番号が見つかりません" in text:
            return "NOT_FOUND"

        if "お届け済み" in text:
            return "DELIVERED"

        return "IN_TRANSIT"

    except Exception as e:
        print("ERROR:", e)
        return None


# ===== COMMANDS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📦 Dùng lệnh:\n\n"
        "/add <mã>\n"
        "/list\n"
        "/remove <code>\n"
        "/removeall (admin)"
    )


# ===== ADD =====
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Dùng: /add <mã vận đơn>")
        return

    tracking = context.args[0].strip().replace(" ", "")

    cursor.execute(
        "SELECT * FROM trackings WHERE tracking_number=? AND chat_id=?",
        (tracking, chat_id)
    )
    if cursor.fetchone():
        await update.message.reply_text("⚠️ Đã tồn tại mã này")
        return

    status = get_tracking_status(tracking)

    if status is None:
        await update.message.reply_text("❌ Lỗi kiểm tra mã")
        return

    cursor.execute(
        "INSERT INTO trackings (tracking_number, chat_id, user_id, username, last_status) VALUES (?, ?, ?, ?, ?)",
        (tracking, chat_id, user.id, user.username, status)
    )
    conn.commit()

    if status == "NOT_FOUND":
        await update.message.reply_text(f"📦 {tracking}\n⚠️ Hệ thống chưa nhận đơn")
    elif status == "DELIVERED":
        await update.message.reply_text(f"📦 {tracking}\n✅ Đã giao rồi")
    else:
        await update.message.reply_text(f"📦 {tracking}\n🚚 Đang vận chuyển\n🔔 Sẽ báo khi giao xong")


# ===== LIST =====
async def list_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    cursor.execute(
        "SELECT tracking_number, last_status FROM trackings WHERE chat_id=?",
        (chat_id,)
    )
    rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("📭 Không có đơn nào")
        return

    msg = "📦 Danh sách:\n\n"
    for t, s in rows:
        if s == "DELIVERED":
            status_text = "✅ Đã giao"
        elif s == "NOT_FOUND":
            status_text = "⚠️ Chưa nhận"
        else:
            status_text = "🚚 Đang đi"

        msg += f"{t} → {status_text}\n"

    await update.message.reply_text(msg)


# ===== REMOVE =====
async def remove_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("Dùng: /remove <tracking>")
        return

    tracking = context.args[0]

    cursor.execute(
        "DELETE FROM trackings WHERE tracking_number=? AND chat_id=?",
        (tracking, chat_id)
    )
    conn.commit()

    await update.message.reply_text(f"🗑 Đã xoá {tracking}")


# ===== REMOVE ALL =====
async def remove_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    member = await chat.get_member(user.id)

    if member.status not in ["administrator", "creator"]:
        await update.message.reply_text("❌ Chỉ admin mới dùng lệnh này")
        return

    cursor.execute("DELETE FROM trackings WHERE chat_id=?", (chat.id,))
    conn.commit()

    await update.message.reply_text("🗑 Đã xoá toàn bộ tracking")


# ===== JOB CHECK =====
async def job_check(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute(
        "SELECT id, tracking_number, chat_id, user_id, username, last_status FROM trackings"
    )
    rows = cursor.fetchall()

    for row_id, tracking, chat_id, user_id, username, old_status in rows:
        new_status = get_tracking_status(tracking)

        if not new_status:
            continue

        # ===== mới được tiếp nhận =====
        if new_status == "IN_TRANSIT" and old_status == "NOT_FOUND":
            cursor.execute(
                "UPDATE trackings SET last_status=? WHERE id=?",
                (new_status, row_id)
            )
            conn.commit()

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📦 {tracking}\n📮 Đơn đã được tiếp nhận bởi Japan Post"
            )

        # ===== đã giao =====
        if new_status == "DELIVERED" and old_status != "DELIVERED":
            cursor.execute(
                "UPDATE trackings SET last_status=? WHERE id=?",
                (new_status, row_id)
            )
            conn.commit()

            if username:
                tag = f"@{username}"
                parse_mode = None
            else:
                tag = f"<a href='tg://user?id={user_id}'>Người dùng</a>"
                parse_mode = "HTML"

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📦 {tracking}\n🎉 ĐÃ GIAO HÀNG!\n👤 {tag}",
                parse_mode=parse_mode
            )


# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN)\
        .connect_timeout(30)\
        .read_timeout(30)\
        .build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("list", list_tracking))
    app.add_handler(CommandHandler("remove", remove_tracking))
    app.add_handler(CommandHandler("removeall", remove_all))

    app.job_queue.run_repeating(job_check, interval=CHECK_INTERVAL, first=10)

    print("BOT RUNNING...")
    app.run_polling()


if __name__ == "__main__":
    main()
