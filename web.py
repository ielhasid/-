"""
שרת האתר לקביעת תורים - שלב 3.

השרת עושה שני דברים:
1. מגיש את העמוד (static/index.html) ללקוחות.
2. מספק API - כתובות שהעמוד פונה אליהן ב-JavaScript כדי לקבל נתונים ולקבוע תור.
   כל ה"מוח" נשאר ב-scheduling.py; השרת רק מתרגם בין האינטרנט לפונקציות שלנו.

הרצה:  python web.py   ואז לפתוח בדפדפן  http://localhost:5000
"""

import json
import re
import threading
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory

import config
from db import init_db, connection
from scheduling import get_available_slots, book_appointment, get_addon, now_local, BookingError

DAYS_AHEAD = 14
HEB_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # לפי date.weekday()

app = Flask(__name__, static_folder="static")


# ---------- עזרים ----------

def _bool_arg(name):
    return request.args.get(name, "0") in ("1", "true")


def _int_arg(name):
    try:
        return int(request.args[name])
    except (KeyError, ValueError):
        raise BookingError(f"חסר או לא תקין: {name}")


def normalize_phone(raw: str) -> str | None:
    """מקבל טלפון בכל צורה ('050-123 4567', '+972501234567') ומחזיר 0501234567, או None אם לא תקין."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    return digits if re.fullmatch(r"05\d{8}", digits) else None


def notify_owner(text: str):
    """שולח הודעה ליובל בטלגרם. רץ ברקע, כדי שהלקוחה לא תחכה; כישלון לא מפיל את ההזמנה."""
    token, chat_id = getattr(config, "BOT_TOKEN", None), getattr(config, "OWNER_CHAT_ID", None)
    if not token or not chat_id or "PASTE" in token:
        return

    def send():
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
        except Exception as e:  # noqa: BLE001 - התראה היא "נחמד שיהיה", לא קריטית
            app.logger.warning("Telegram notification failed: %s", e)

    threading.Thread(target=send, daemon=True).start()


@app.errorhandler(BookingError)
def booking_error(e):
    return jsonify(error=str(e)), 400


# ---------- העמוד ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- API ----------

@app.get("/api/info")
def info():
    with connection() as conn:
        services = conn.execute(
            "SELECT id, name, price, duration_minutes FROM services WHERE is_addon = 0 ORDER BY id"
        ).fetchall()
        addon = get_addon(conn)
    return jsonify(
        services=[dict(s) for s in services],
        addon=dict(addon) if addon else None,
        studio_address=getattr(config, "STUDIO_ADDRESS", None),
        whatsapp=getattr(config, "BUSINESS_WHATSAPP", None),
    )


@app.get("/api/days")
def days():
    """הימים הקרובים, ולכל יום: האם יש בו מקום."""
    service_id, with_addon = _int_arg("service_id"), _bool_arg("addon")
    today = now_local().date()
    result = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        result.append({
            "date": d.isoformat(),
            "weekday": HEB_WEEKDAYS[d.weekday()],
            "available": bool(get_available_slots(service_id, d, with_addon=with_addon)),
        })
    return jsonify(days=result)


@app.get("/api/slots")
def slots():
    service_id, with_addon = _int_arg("service_id"), _bool_arg("addon")
    try:
        d = date.fromisoformat(request.args.get("date", ""))
    except ValueError:
        raise BookingError("תאריך לא תקין")
    times = get_available_slots(service_id, d, with_addon=with_addon)
    return jsonify(slots=[t.strftime("%H:%M") for t in times])


@app.post("/api/book")
def book():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = normalize_phone(data.get("phone", ""))
    if len(name) < 2:
        raise BookingError("צריך שם כדי לקבוע תור")
    if not phone:
        raise BookingError("מספר הטלפון לא נראה תקין")
    try:
        service_id = int(data["service_id"])
        start = datetime.fromisoformat(data["start"])   # "2026-10-08T16:30"
    except (KeyError, ValueError, TypeError):
        raise BookingError("פרטי התור חסרים")
    with_addon = bool(data.get("addon"))

    appt_id = book_appointment(name, service_id, start, customer_phone=phone, with_addon=with_addon)

    with connection() as conn:
        row = conn.execute(
            "SELECT a.start_time, a.end_time, s.name, s.price FROM appointments a "
            "JOIN services s ON s.id = a.service_id WHERE a.id = ?", (appt_id,)
        ).fetchone()
        addon = get_addon(conn) if with_addon else None

    service_name = row["name"] + (f" + {addon['name']}" if addon else "")
    total = row["price"] + (addon["price"] if addon else 0)
    day_label = f"יום {HEB_WEEKDAYS[start.weekday()]} {start.strftime('%d/%m')}"
    notify_owner(f"🔔 תור חדש מהאתר\n{name} · {phone}\n{service_name}\n{day_label} בשעה {start.strftime('%H:%M')}")

    return jsonify(
        id=appt_id, service=service_name, total=total,
        start=row["start_time"], end=row["end_time"],
    ), 201


if __name__ == "__main__":
    init_db()
    print("האתר רץ: http://localhost:5000   (לעצירה: Ctrl+C)")
    app.run(port=5000, debug=False)
