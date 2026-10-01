"""
בוט טלגרם לקביעת תורים - שלב 2.

הזרימה ללקוחה:
  /start  ->  בחירת שירות  ->  בחירת יום  ->  בחירת שעה  ->  אישור  ->  התור נקבע

כל לחיצה על כפתור שולחת לבוט "callback_data" - מחרוזת קצרה שאנחנו מגדירים,
למשל "day:2:2026-10-04" (= שירות 2, יום 4.10). הפונקציה on_button מפרקת
את המחרוזת ומחליטה מה להציג הלאה.

הרצה:  python bot.py
"""

import logging
from datetime import date, datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

import config
from db import init_db, connection
from scheduling import get_available_slots, book_appointment, BookingError, now_local

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

DAYS_AHEAD = 14          # כמה ימים קדימה מציעים
SLOTS_PER_ROW = 4
HEB_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # לפי date.weekday()


# ---------- עזרים ----------

def _services():
    with connection() as conn:
        return conn.execute(
            "SELECT id, name, price, duration_minutes FROM services WHERE is_addon = 0 ORDER BY id"
        ).fetchall()


def _service(service_id):
    with connection() as conn:
        return conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()


def _day_label(d: date) -> str:
    return f"יום {HEB_WEEKDAYS[d.weekday()]} {d.strftime('%d/%m')}"


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


# ---------- המסכים ----------

def services_keyboard():
    rows = [
        [InlineKeyboardButton(f"{s['name']} · {s['price']} ₪", callback_data=f"svc:{s['id']}")]
        for s in _services()
    ]
    return InlineKeyboardMarkup(rows)


def days_keyboard(service_id):
    today = now_local().date()
    rows = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if get_available_slots(service_id, d):          # מציגים רק ימים שיש בהם מקום
            rows.append([InlineKeyboardButton(_day_label(d), callback_data=f"day:{service_id}:{d.isoformat()}")])
    rows.append([InlineKeyboardButton("« חזרה לשירותים", callback_data="back")])
    return InlineKeyboardMarkup(rows), len(rows) > 1


def slots_keyboard(service_id, d: date):
    slots = get_available_slots(service_id, d)
    buttons = [
        InlineKeyboardButton(s.strftime("%H:%M"), callback_data=f"slot:{service_id}:{s.strftime('%Y-%m-%dT%H:%M')}")
        for s in slots
    ]
    rows = _chunks(buttons, SLOTS_PER_ROW)
    rows.append([InlineKeyboardButton("« חזרה לימים", callback_data=f"svc:{service_id}")])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(service_id, start_iso):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ אישור", callback_data=f"ok:{service_id}:{start_iso}"),
        InlineKeyboardButton("❌ ביטול", callback_data="back"),
    ]])


# ---------- פקודות ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "היי! 💅 כאן קובעים תור אצל יובל.\nאיזה טיפול תרצי?",
        reply_markup=services_keyboard(),
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ה-chat id שלך: {update.effective_chat.id}")


# ---------- לחיצות על כפתורים ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()                 # מעלים את "השעון" הקטן על הכפתור
    action, *args = query.data.split(":", 2)

    if action == "back":
        await query.edit_message_text("איזה טיפול תרצי?", reply_markup=services_keyboard())

    elif action == "svc":
        service_id = int(args[0])
        keyboard, has_days = days_keyboard(service_id)
        text = "באיזה יום?" if has_days else "אין תורים פנויים בשבועיים הקרובים 😔"
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action == "day":
        service_id, d = int(args[0]), date.fromisoformat(args[1])
        await query.edit_message_text(f"{_day_label(d)} - באיזו שעה?", reply_markup=slots_keyboard(service_id, d))

    elif action == "slot":
        service_id, start_iso = int(args[0]), args[1]
        start_dt = datetime.fromisoformat(start_iso)
        s = _service(service_id)
        await query.edit_message_text(
            f"לאשר את התור?\n\n💅 {s['name']}\n📅 {_day_label(start_dt.date())}\n"
            f"🕐 {start_dt.strftime('%H:%M')}\n💰 {s['price']} ₪",
            reply_markup=confirm_keyboard(service_id, start_iso),
        )

    elif action == "ok":
        await confirm_booking(query, context, int(args[0]), datetime.fromisoformat(args[1]))


async def confirm_booking(query, context, service_id, start_dt):
    user = query.from_user
    try:
        book_appointment(user.full_name, service_id, start_dt, customer_chat=query.message.chat.id)
    except BookingError:
        await query.edit_message_text(
            "אופס, מישהי תפסה את השעה הזו ממש עכשיו 🙈 בואי נבחר שעה אחרת:",
            reply_markup=slots_keyboard(service_id, start_dt.date()),
        )
        return

    s = _service(service_id)
    when = f"{_day_label(start_dt.date())} בשעה {start_dt.strftime('%H:%M')}"
    await query.edit_message_text(f"התור נקבע! ✨\n{s['name']}, {when}.\nנתראה 💖")

    if config.OWNER_CHAT_ID:
        await context.bot.send_message(config.OWNER_CHAT_ID, f"🔔 תור חדש: {user.full_name}\n{s['name']}, {when}")


# ---------- הפעלה ----------

def main():
    init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CallbackQueryHandler(on_button))
    print("הבוט רץ. לעצירה: Ctrl+C")
    app.run_polling()


if __name__ == "__main__":
    main()
