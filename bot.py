import os
import requests
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import psycopg2
from contextlib import contextmanager

# ===== CONFIG =====
TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

CHECK_INTERVAL = 1800  # 30 phút
CLEANUP_INTERVAL = 86400  # Quét dọn dẹp mỗi 24 giờ (1 ngày)

# ===== DATABASE =====
@contextmanager
def get_db():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing!")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS trackings (
                id SERIAL PRIMARY KEY,
                tracking_number VARCHAR(255),
                chat_id BIGINT,
                last_status VARCHAR(255)
            );
            """)
            # Tự động nâng cấp thêm cột nếu chưa có (PostgreSQL hỗ trợ ADD COLUMN IF NOT EXISTS)
            cursor.execute("ALTER TABLE trackings ADD COLUMN IF NOT EXISTS user_id BIGINT;")
            cursor.execute("ALTER TABLE trackings ADD COLUMN IF NOT EXISTS username VARCHAR(255);")
            cursor.execute("ALTER TABLE trackings ADD COLUMN IF NOT EXISTS delivered_at VARCHAR(255);")

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

        # Giao lại
        if "再配達" in text:
            return "REDELIVERY"

        # Người nhận vắng mặt
        absent_keywords = ["ご不在", "持ち戻り", "不在のため"]
        for keyword in absent_keywords:
            if keyword in text:
                return "ABSENT"

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
        "/remove <mã 1> <mã 2> ... (Xóa một hoặc nhiều mã cùng lúc)\n"
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

    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM trackings WHERE tracking_number = %s AND chat_id = %s",
                    (tracking, chat_id)
                )
                exists = cursor.fetchone()
    except Exception as db_err:
        print(f"Lỗi truy vấn DB: {db_err}")
        await update.message.reply_text("❌ Lỗi kiểm tra cơ sở dữ liệu")
        return

    if exists:
        await update.message.reply_text("⚠️ Đã tồn tại mã này")
        return

    status = get_tracking_status(tracking)

    if status is None:
        await update.message.reply_text("❌ Lỗi kiểm tra mã")
        return

    # Nếu mã add vào bưu điện báo đã giao luôn, ghi nhận ngày giờ hiện tại để đếm ngược 1 tuần xóa đơn
    delivered_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "DELIVERED" else None

    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO trackings (tracking_number, chat_id, user_id, username, last_status, delivered_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (tracking, chat_id, user.id, user.username, status, delivered_time)
                )
    except Exception as db_err:
        print(f"Lỗi chèn DB: {db_err}")
        await update.message.reply_text("❌ Lỗi lưu thông tin vào cơ sở dữ liệu")
        return

    if status == "NOT_FOUND":
        await update.message.reply_text(f"📦 {tracking}\n⚠️ Hệ thống chưa nhận đơn")
    elif status == "ABSENT":
        await update.message.reply_text(f"📦 {tracking}\n📭 Người nhận vắng mặt")
    elif status == "REDELIVERY":
        await update.message.reply_text(f"📦 {tracking}\n🔄 Đang giao lại")
    elif status == "DELIVERED":
        await update.message.reply_text(f"📦 {tracking}\n✅ Đã giao rồi\n⏱ Đơn này sẽ tự động biến mất sau 1 tuần")
    else:
        await update.message.reply_text(f"📦 {tracking}\n🚚 Đang vận chuyển\n🔔 Sẽ báo khi giao xong")


# ===== LIST =====
async def list_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT tracking_number, last_status FROM trackings WHERE chat_id = %s",
                    (chat_id,)
                )
                rows = cursor.fetchall()
    except Exception as db_err:
        print(f"Lỗi truy vấn danh sách: {db_err}")
        await update.message.reply_text("❌ Lỗi truy xuất cơ sở dữ liệu")
        return

    if not rows:
        await update.message.reply_text("📭 Không có đơn nào")
        return

    msg = "📦 Danh sách:\n\n"
    for t, s in rows:
        if s == "DELIVERED":
            status_text = "✅ Đã giao"
        elif s == "REDELIVERY":
            status_text = "🔄 Đang giao lại"
        elif s == "ABSENT":
            status_text = "📭 Người nhận vắng mặt"
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
        await update.message.reply_text("Dùng: /remove <mã 1> <mã 2> ...")
        return

    # Lấy toàn bộ danh sách mã được truyền vào sau lệnh, dọn dẹp khoảng trắng thừa
    trackings_to_remove = [t.strip() for t in context.args if t.strip()]
    removed_list = []
    
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                for tracking in trackings_to_remove:
                    cursor.execute(
                        "SELECT 1 FROM trackings WHERE tracking_number = %s AND chat_id = %s",
                        (tracking, chat_id)
                    )
                    if cursor.fetchone():
                        cursor.execute(
                            "DELETE FROM trackings WHERE tracking_number = %s AND chat_id = %s",
                            (tracking, chat_id)
                        )
                        removed_list.append(tracking)
    except Exception as db_err:
        print(f"Lỗi xóa tracking: {db_err}")
        await update.message.reply_text("❌ Lỗi khi xóa mã vận đơn trong cơ sở dữ liệu")
        return

    if not removed_list:
        await update.message.reply_text("⚠️ Không tìm thấy mã nào trùng khớp trong danh sách của bạn để xóa.")
    else:
        msg = f"🗑 **Đã xóa thành công {len(removed_list)} mã vận đơn:**\n\n"
        for t in removed_list:
            msg += f"• `{t}`\n"
        await update.message.reply_text(msg, parse_mode="Markdown")


# ===== REMOVE ALL =====
async def remove_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    member = await chat.get_member(user.id)

    if member.status not in ["administrator", "creator"]:
        await update.message.reply_text("❌ Chỉ admin mới dùng lệnh này")
        return

    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM trackings WHERE chat_id = %s", (chat.id,))
    except Exception as db_err:
        print(f"Lỗi xóa tất cả: {db_err}")
        await update.message.reply_text("❌ Lỗi khi xóa toàn bộ tracking")
        return

    await update.message.reply_text("🗑 Đã xoá toàn bộ tracking")


# ===== JOB CHECK =====
async def job_check(context: ContextTypes.DEFAULT_TYPE):
    # Lấy nhanh toàn bộ mã để tránh giữ transaction hoặc connection quá lâu khi thực hiện HTTP request
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, tracking_number, chat_id, user_id, username, last_status FROM trackings"
                )
                rows = cursor.fetchall()
    except Exception as e:
        print(f"Lỗi truy vấn job_check: {e}")
        return

    for row_id, tracking, chat_id, user_id, username, old_status in rows:
        new_status = get_tracking_status(tracking)

        if not new_status or new_status == old_status:
            continue

        # Thiết lập thẻ tag người dùng an toàn
        if username:
            tag = f"@{username}"
            parse_mode = None
        else:
            tag = f"<a href='tg://user?id={user_id}'>Người dùng</a>"
            parse_mode = "HTML"

        is_updated = False
        message_text = ""

        # 1. Mới được tiếp nhận
        if new_status == "IN_TRANSIT" and old_status == "NOT_FOUND":
            message_text = f"📦 {tracking}\n📮 Đơn đã được tiếp nhận bởi Japan Post"
            is_updated = True

        # 2. Người nhận vắng mặt
        elif new_status == "ABSENT":
            message_text = (
                f"📦 {tracking}\n"
                f"📭 NGƯỜI NHẬN VẮNG MẶT\n"
                f"👤 {tag}\n\n"
                f"⚠️ Japan Post đã phát hàng nhưng không gặp người nhận."
            )
            is_updated = True

        # 3. Đang giao lại
        elif new_status == "REDELIVERY":
            message_text = (
                f"📦 {tracking}\n"
                f"🔄 ĐANG GIAO LẠI\n"
                f"👤 {tag}\n\n"
                f"📮 Japan Post đang thực hiện giao lại đơn hàng."
            )
            is_updated = True

        # 4. Đã giao thành công
        elif new_status == "DELIVERED":
            message_text = f"📦 {tracking}\n🎉 ĐÃ GIAO HÀNG!\n👤 {tag}"
            is_updated = True
        
        # 5. Các cập nhật trạng thái khác
        else:
            is_updated = True

        # Tiến hành cập nhật DB và gửi tin nhắn nếu có thay đổi hợp lệ
        if is_updated:
            try:
                # Thực hiện cập nhật DB trên transaction rất ngắn
                with get_db() as conn:
                    with conn.cursor() as cursor:
                        if new_status == "DELIVERED":
                            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            cursor.execute(
                                "UPDATE trackings SET last_status = %s, delivered_at = %s WHERE id = %s",
                                (new_status, now_str, row_id)
                            )
                        else:
                            cursor.execute(
                                "UPDATE trackings SET last_status = %s WHERE id = %s",
                                (new_status, row_id)
                            )
            except Exception as db_err:
                print(f"Lỗi cập nhật DB cho {tracking}: {db_err}")
                continue

            if message_text:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=message_text,
                        parse_mode=parse_mode
                    )
                except Exception as send_err:
                    print(f"Lỗi gửi tin nhắn cho chat_id {chat_id}: {send_err}")


# ===== JOB CLEANUP (TỰ ĐỘNG XÓA ĐƠN ĐÃ GIAO ĐƯỢC 1 TUẦN) =====
async def job_cleanup(context: ContextTypes.DEFAULT_TYPE):
    # Tính mốc thời gian trước đây chính xác 7 ngày
    one_week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM trackings WHERE last_status = 'DELIVERED' AND delivered_at <= %s",
                    (one_week_ago,)
                )
        print("🤖 [Hệ thống] Đã tự động dọn dẹp các đơn hàng giao thành công sau 1 tuần.")
    except Exception as e:
        print(f"Lỗi dọn dẹp database: {e}")


# ===== MAIN =====
def main():
    if not TOKEN:
        print("❌ LỖI: Thiếu biến môi trường 'TOKEN'!")
        return

    # Khởi tạo db
    try:
        init_db()
        print("✅ DATABASE INITIALIZED SUCCESSFULLY")
    except Exception as db_err:
        print(f"❌ LỖI KHỞI TẠO DATABASE: {db_err}")
        return

    app = ApplicationBuilder().token(TOKEN)\
        .connect_timeout(30)\
        .read_timeout(30)\
        .build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("list", list_tracking))
    app.add_handler(CommandHandler("remove", remove_tracking))
    app.add_handler(CommandHandler("removeall", remove_all))

    # Vòng lặp quét bưu điện (30 phút / lần)
    app.job_queue.run_repeating(job_check, interval=CHECK_INTERVAL, first=10)
    
    # Vòng lặp tự quét dọn dẹp hệ thống dữ liệu cũ (24 giờ / lần)
    app.job_queue.run_repeating(job_cleanup, interval=CLEANUP_INTERVAL, first=30)

    print("BOT RUNNING...")
    
    # Đã thêm tham số drop_pending_updates=True để gỡ hoàn toàn tình trạng kẹt session cũ
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
