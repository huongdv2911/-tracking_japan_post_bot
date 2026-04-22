import requests
import sqlite3
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ===== CONFIG =====
TOKEN = "8679493877:AAGuFYwzIQ4Eelmi9ySNeEU3bAQ3fG-3NbI"
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


# ===== CHECK STATUS JAPAN POST =====
def get_tracking_status(tracking_number):
    url = "https://trackings.post.japanpost.jp/services/srv/search/direct"

    payload = {
        "reqCodeNo1": tracking_number,
        "searchKind": "S002",
        "locale": "ja"
    }

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    try:
        res = requests.post(url, data=payload, headers=headers, timeout=15)
        text = res.text

        # ===== ĐÃ GIAO =====
        if "お届け済み" in text or "お届け先にお届け済み" in text:
            return "DELIVERED"

        return "IN_TRANSIT"

    except Exception as e:
        print("ERROR:", e)
        return None


# ===== COMMANDS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📦 Gửi mã Japan Post\n\n"
        "🔔 Bot sẽ báo khi ĐÃ GIAO\n\n"
        "/list - xem\n"
        "/remove <code> - xoá 1 mã\n"
        "/removeall - xoá tất cả"
    )


async def add_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracking = update.message.text.strip().replace(" ", "")
    chat_id = update.effective_chat.id

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
        "INSERT INTO trackings (tracking_number, chat_id, last_status) VALUES (?, ?, ?)",
        (tracking, chat_id, status)
    )
    conn.commit()

    if status == "DELIVERED":
        await update.message.reply_text(f"📦 {tracking}\n✅ Đã giao rồi")
    else:
        await update.message.reply_text(f"📦 {tracking}\n🚚 Đang vận chuyển\n🔔 Sẽ báo khi giao xong")


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
        status_text = "✅ Đã giao" if s == "DELIVERED" else "🚚 Đang đi"
        msg += f"{t} → {status_text}\n"

    await update.message.reply_text(msg)


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


async def remove_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    cursor.execute(
        "DELETE FROM trackings WHERE chat_id=?",
        (chat_id,)
    )
    conn.commit()

    await update.message.reply_text("🗑 Đã xoá toàn bộ tracking")


# ===== JOB CHECK =====
async def job_check(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT id, tracking_number, chat_id, last_status FROM trackings")
    rows = cursor.fetchall()

    for row_id, tracking, chat_id, old_status in rows:
        new_status = get_tracking_status(tracking)

        if not new_status:
            continue

        if new_status == "DELIVERED" and old_status != "DELIVERED":
            cursor.execute(
                "UPDATE trackings SET last_status=? WHERE id=?",
                (new_status, row_id)
            )
            conn.commit()

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📦 {tracking}\n🎉 ĐÃ GIAO HÀNG!"
                )
            except Exception as e:
                print("Send error:", e)


# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN)\
        .connect_timeout(30)\
        .read_timeout(30)\
        .build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_tracking))
    app.add_handler(CommandHandler("remove", remove_tracking))
    app.add_handler(CommandHandler("removeall", remove_all))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_tracking))

    app.job_queue.run_repeating(job_check, interval=CHECK_INTERVAL, first=10)

    print("BOT RUNNING...")
    app.run_polling()


if __name__ == "__main__":
    main()