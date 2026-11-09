"""
שרת האתר לקביעת תורים.

השרת עושה שלושה דברים:
1. מגיש את העמוד ללקוחות (static/index.html) ואת עמוד הניהול של יובל (static/admin.html).
2. מספק API - כתובות שהעמודים פונים אליהן ב-JavaScript כדי לקבל נתונים ולקבוע תור.
3. מגן על כתובות הניהול בסיסמה.
כל ה"מוח" של התורים נשאר ב-scheduling.py; השרת רק מתרגם בין האינטרנט לפונקציות שלנו.

הרצה:  python web.py   ואז לפתוח בדפדפן  http://localhost:5000  (וניהול: /admin)
"""

import hashlib
import hmac
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

from flask import Flask, Response, jsonify, request, send_from_directory, session

import config
from db import init_db, connection
from scheduling import (get_available_slots, book_appointment, cancel_appointment, confirm_appointment, get_addon,
                        now_local, get_day_windows, free_gaps, unset_days_mode, BookingError, MAX_ADDON,
                        TIME_FMT, ACTIVE, BUSINESS_TZ)

HEB_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # לפי date.weekday()

app = Flask(__name__, static_folder="static")


def _admin_password():
    return getattr(config, "ADMIN_PASSWORD", None)


# מפתח להצפנת עוגיית ההתחברות. נגזר מהסיסמה, כך שהחלפת סיסמה מנתקת את כל מי שהיה מחובר.
app.secret_key = hashlib.sha256(f"yuvalnails::{_admin_password() or 'no-admin'}".encode()).hexdigest()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)


# ---------- עזרים ----------

def _count_arg(name):
    """?addon=2 -> 2. גם "true" (מגרסה ישנה של העמוד) נחשב 1."""
    value = request.args.get(name, "0")
    return 1 if value == "true" else value


def _int_arg(name):
    try:
        return int(request.args[name])
    except (KeyError, ValueError):
        raise BookingError(f"חסר או לא תקין: {name}")


def _date_arg(name="date"):
    try:
        return date.fromisoformat(request.args.get(name, ""))
    except ValueError:
        raise BookingError("תאריך לא תקין")


def default_booking_until(today: date) -> date:
    """ברירת מחדל: עד סוף דצמבר של השנה הנוכחית. יובל יכולה לשנות בעמוד הניהול."""
    return date(today.year, 12, 31)


def last_bookable_day(conn=None) -> date:
    """היום האחרון שבו לקוחות יכולות לקבוע תור (מוגדר ע"י יובל, או סוף דצמבר)."""
    if conn is None:
        with connection() as conn:
            return last_bookable_day(conn)
    today = now_local().date()
    row = conn.execute("SELECT value FROM settings WHERE key = 'booking_until'").fetchone()
    try:
        until = date.fromisoformat(row["value"]) if row else default_booking_until(today)
    except ValueError:
        until = default_booking_until(today)
    return max(until, today)


def _check_in_range(d: date):
    last = last_bookable_day()
    if d > last:
        raise BookingError(f"אפשר לקבוע תורים עד {last.day}.{last.month}.{last.year}")


def normalize_phone(raw: str) -> str | None:
    """מקבל טלפון בכל צורה ('050-123 4567', '+972501234567') ומחזיר 0501234567, או None אם לא תקין."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    return digits if re.fullmatch(r"05\d{8}", digits) else None


def addon_label(addon, count) -> str:
    """" + השלמת ציפורן" / " + 3 השלמות ציפורן" / "" """
    if not addon or not count:
        return ""
    return f" + {addon['name']}" if count == 1 else f" + {count} השלמות ציפורן"


def day_label(d) -> str:
    return f"יום {HEB_WEEKDAYS[d.weekday()]} {d.day}.{d.month}"


def owner_chat_ids() -> list:
    """
    למי שולחים התראות. ב-config.py אפשר מספר אחד או כמה:
        OWNER_CHAT_ID = 123456789
        OWNER_CHAT_ID = [123456789, 987654321]     # למשל גם לך וגם ליובל
    """
    raw = getattr(config, "OWNER_CHAT_ID", None)
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [i for i in items if i]


def notify_owner(text: str):
    """שולח הודעה ליובל (ולכל מי שברשימה) בטלגרם. רץ ברקע, כדי שהלקוחה לא תחכה; כישלון לא מפיל את ההזמנה."""
    token, chat_ids = getattr(config, "BOT_TOKEN", None), owner_chat_ids()
    if not token or not chat_ids or "PASTE" in token:
        return

    def send():
        for chat_id in chat_ids:                 # כל אחד בנפרד: אם אחד נכשל, השני עדיין מקבל
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            try:
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
            except Exception as e:  # noqa: BLE001 - התראה היא "נחמד שיהיה", לא קריטית
                app.logger.warning("Telegram notification to %s failed: %s", chat_id, e)

    threading.Thread(target=send, daemon=True).start()


@app.errorhandler(BookingError)
def booking_error(e):
    return jsonify(error=str(e)), 400


# ---------- העמודים ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/admin")
def admin_page():
    return send_from_directory(app.static_folder, "admin.html")


# ---------- API ללקוחות ----------

@app.get("/api/info")
def info():
    with connection() as conn:
        services = conn.execute(
            # בלי מחירים: העמוד ללקוחות לא מציג מחירים (הלקוחות הקבועות כבר מכירות אותם)
            "SELECT id, name, duration_minutes, refill_days, allows_addon FROM services WHERE is_addon = 0 ORDER BY id"
        ).fetchall()
        addon = get_addon(conn)
    return jsonify(
        services=[dict(s) for s in services],
        addon={k: addon[k] for k in ("id", "name", "duration_minutes", "price")} if addon else None,
        max_addon=MAX_ADDON,
        studio_address=getattr(config, "STUDIO_ADDRESS", None),
        whatsapp=getattr(config, "BUSINESS_WHATSAPP", None),
        today=now_local().date().isoformat(),
        last_day=last_bookable_day().isoformat(),
    )


@app.get("/api/month")
def month():
    """
    כל הימים בחודש, ולכל יום סטטוס:
    available = יש מקום · none = אין תורים פנויים · past = עבר · out_of_range = רחוק מדי
    יום מלא ויום שיובל לא עובדת מקבלים אותו סטטוס, כדי שלקוחות לא יוכלו לדעת כמה היא עמוסה.
    """
    service_id, addon_count = _int_arg("service_id"), _count_arg("addon")
    try:
        year, mon = map(int, request.args.get("month", "").split("-"))
        first = date(year, mon, 1)
    except ValueError:
        raise BookingError("חודש לא תקין")
    today, last = now_local().date(), last_bookable_day()
    next_month = date(year + (mon == 12), mon % 12 + 1, 1)

    days = []
    with connection() as conn:                     # חיבור אחד לכל החודש, במקום 30 חיבורים
        d = first
        while d < next_month:
            if d < today:
                status = "past"
            elif d > last:
                status = "out_of_range"
            elif get_available_slots(service_id, d, conn=conn, addon_count=addon_count):
                status = "available"
            else:
                status = "none"
            days.append({"date": d.isoformat(), "status": status})
            d += timedelta(days=1)
    return jsonify(month=first.strftime("%Y-%m"), days=days)


@app.get("/api/slots")
def slots():
    service_id, addon_count = _int_arg("service_id"), _count_arg("addon")
    d = _date_arg()
    _check_in_range(d)
    times = get_available_slots(service_id, d, addon_count=addon_count)
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
    _check_in_range(start.date())
    addon_count = data.get("addon") or 0
    if addon_count is True:
        addon_count = 1
    is_next = bool(data.get("is_next"))

    # כל תור מהאתר הוא "בקשה" עד שיובל מאשרת. השעה כבר שמורה, כדי שאף אחת אחרת לא תתפוס אותה.
    token = secrets.token_urlsafe(12)
    appt_id = book_appointment(name, service_id, start, customer_phone=phone, addon_count=addon_count,
                               status="pending", public_token=token)
    info = _booking_details(appt_id)

    title = "🔁 בקשה לתור הבא" if is_next else "🔔 בקשה לתור חדש"
    notify_owner(f"{title}\n{name} · {phone}\n{info['service']}\n{day_label(start)} בשעה {start.strftime('%H:%M')}"
                 f"\n\nמחכה לאישור שלך בעמוד הניהול ✅")

    return jsonify(id=appt_id, token=token, **info), 201


def _booking_details(appt_id) -> dict:
    with connection() as conn:
        row = conn.execute(
            "SELECT a.start_time, a.end_time, a.with_addon, a.status, s.name FROM appointments a "
            "JOIN services s ON s.id = a.service_id WHERE a.id = ?", (appt_id,)
        ).fetchone()
        addon = get_addon(conn)
    count = row["with_addon"]
    return dict(
        service=row["name"] + addon_label(addon, count),
        start=row["start_time"], end=row["end_time"], status=row["status"],
    )


@app.get("/api/booking/<token>")
def booking_status(token):
    """הקישור האישי של הלקוחה: האם יובל כבר אישרה את התור."""
    with connection() as conn:
        row = conn.execute("SELECT id, customer_name FROM appointments WHERE public_token = ?", (token,)).fetchone()
    if not row:
        return jsonify(error="התור לא נמצא"), 404
    return jsonify(name=row["customer_name"].split()[0], studio_address=getattr(config, "STUDIO_ADDRESS", None),
                   directions=getattr(config, "STUDIO_DIRECTIONS", None),
                   whatsapp=getattr(config, "BUSINESS_WHATSAPP", None), **_booking_details(row["id"]))


# ---------- ניהול (רק ליובל) ----------

def admin_required(view):
    """מגן על כתובת: רק מי שהתחברה עם הסיסמה מקבלת גישה."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _admin_password():
            return jsonify(error="עמוד הניהול לא מופעל: חסרה ADMIN_PASSWORD ב-config.py"), 503
        if not session.get("admin"):
            return jsonify(error="צריך להתחבר"), 401
        if request.method == "POST" and not request.is_json:
            # מגן מפני אתרים זרים ששולחים טפסים בשם הדפדפן של יובל
            return jsonify(error="בקשה לא תקינה"), 400
        return view(*args, **kwargs)
    return wrapper


def whatsapp_number(phone: str) -> str:
    """0501234567 -> 972501234567 (הפורמט ש-wa.me מבין)."""
    return "972" + phone[1:] if phone and phone.startswith("0") else phone


def reminder_text(row) -> str:
    start = datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M")
    address = getattr(config, "STUDIO_ADDRESS", None)
    first_name = row["customer_name"].split()[0]          # "היי שירה" נשמע יותר אישי מ"היי שירה כהן"
    text = (f"היי {first_name}, תזכורת לתור שלך מחר, {day_label(start)}, "
            f"בשעה {start.strftime('%H:%M')} ל{row['service']} 💅")
    if address:
        text += f"\nהכתובת: {address}"
    return text + "\nאם צריך לשנות או לבטל, אפשר פשוט להשיב להודעה הזו. מחכה לך! יובל"


def public_root() -> str:
    """כתובת האתר לקישורים שנשלחים החוצה. באתר האמיתי תמיד https (גם אם השרת רואה http מאחורי הקלעים)."""
    root = request.url_root
    if not request.host.startswith(("localhost", "127.0.0.1")):
        root = re.sub(r"^http://", "https://", root)
    return root


def _wa_url(phone, text):
    return f"https://wa.me/{whatsapp_number(phone)}?text={urllib.parse.quote(text)}" if phone else None


def price_line(row) -> str:
    """'עלות התור: 160 ₪ (140 ₪ + 2 השלמות ציפורן × 10 ₪)'. בלי השלמות - רק הסכום."""
    count = row.get("with_addon") or 0
    line = f"עלות התור: {row['total']} ₪"
    if count:
        what = "השלמת ציפורן" if count == 1 else f"{count} השלמות ציפורן × {row['addon_price']} ₪"
        extra = f"{row['addon_price']} ₪" if count == 1 else ""
        line += f" ({row['service_price']} ₪ + {what}{' ' + extra if extra else ''})"
    return line


def approved_text(row) -> str:
    """
    הודעת האישור שיובל שולחת בוואטסאפ. הכתובת וההוראות מגיעות מ-config.py:
        STUDIO_ADDRESS = "יהושע רבינוביץ 53"
        STUDIO_DIRECTIONS = "קומה 5 דירה 22 (ימינה במעלית)\nישנה חניה ברחוב וגם במרכז דרגת."
    (\n = ירידת שורה)
    """
    start = datetime.strptime(row["start_time"], TIME_FMT)
    address = getattr(config, "STUDIO_ADDRESS", None)
    directions = getattr(config, "STUDIO_DIRECTIONS", None)
    lines = [f"היי {row['customer_name'].split()[0]}, התור שלך אושר ✅",
             f"{day_label(start)} בשעה {start.strftime('%H:%M')} ל{row['service']} 💅"]
    if address:
        lines.append(f"הכתובת: {address}")
    if directions:
        lines.append(directions)
    lines += [price_line(row), "*תשלום במזומן במעמד הטיפול.*"]      # כוכביות = הדגשה בוואטסאפ
    if row.get("public_token"):
        # הקישור האישי: שם יש כפתור "הוספה ליומן" (אייפון) ו-Google Calendar (אנדרואיד)
        lines.append(f"📅 להוספה ליומן: {public_root()}?booking={row['public_token']}")
    lines.append("מחכה לך! יובל")
    return "\n".join(lines)


def declined_text(row) -> str:
    start = datetime.strptime(row["start_time"], TIME_FMT)
    return (f"היי {row['customer_name'].split()[0]}, לצערי לא אוכל לקבל אותך ב{day_label(start)} "
            f"בשעה {start.strftime('%H:%M')} 🙏\nאשמח למצוא לך מועד אחר, מתי נוח לך? יובל")


def _appointment_row(conn, appt_id):
    row = conn.execute(
        "SELECT a.*, s.name AS service_name, s.price AS service_price FROM appointments a "
        "JOIN services s ON s.id = a.service_id WHERE a.id = ?", (appt_id,)).fetchone()
    if not row:
        return None
    addon = get_addon(conn)
    addon_price = addon["price"] if addon else 0
    return {**dict(row), "service": row["service_name"] + addon_label(addon, row["with_addon"]),
            "addon_price": addon_price,
            "total": row["service_price"] + addon_price * row["with_addon"]}


@app.post("/api/admin/login")
def admin_login():
    password = _admin_password()
    if not password:
        return jsonify(error="עמוד הניהול לא מופעל: חסרה ADMIN_PASSWORD ב-config.py"), 503
    given = (request.get_json(silent=True) or {}).get("password", "")
    if not hmac.compare_digest(str(given).encode(), str(password).encode()):   # השוואה שלא מדליפה מידע דרך זמן
        time.sleep(1)                                  # מאט ניסיונות לנחש סיסמה
        return jsonify(error="סיסמה שגויה"), 401
    session.permanent = True
    session["admin"] = True
    return jsonify(ok=True)


@app.post("/api/admin/logout")
def admin_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/admin/appointments")
@admin_required
def admin_appointments():
    d = _date_arg()
    with connection() as conn:
        rows = conn.execute(
            "SELECT a.id, a.customer_name, a.customer_phone, a.start_time, a.end_time, a.with_addon, "
            "a.reminder_sent_at, a.status, s.name AS service, s.price "
            "FROM appointments a JOIN services s ON s.id = a.service_id "
            "WHERE a.status IN ('pending', 'confirmed') AND date(a.start_time) = ? ORDER BY a.start_time",
            (d.isoformat(),),
        ).fetchall()
        addon = get_addon(conn)

    result = []
    for r in rows:
        service = r["service"] + addon_label(addon, r["with_addon"])
        item = {
            "id": r["id"], "name": r["customer_name"], "phone": r["customer_phone"],
            "start": r["start_time"][11:], "end": r["end_time"][11:], "service": service,
            "price": r["price"] + (addon["price"] * r["with_addon"] if addon else 0),
            "reminder_sent": bool(r["reminder_sent_at"]),
            "pending": r["status"] == "pending",
        }
        if r["customer_phone"] and r["status"] == "confirmed":
            msg = reminder_text({**dict(r), "service": service})
            item["whatsapp_url"] = f"https://wa.me/{whatsapp_number(r['customer_phone'])}?text={urllib.parse.quote(msg)}"
        result.append(item)
    with connection() as conn:
        blocks = [{"id": b["id"], "start": b["start_time"], "end": b["end_time"], "note": b["note"]}
                  for b in conn.execute("SELECT * FROM blocks WHERE date = ? ORDER BY start_time", (d.isoformat(),))]
        gaps = [{"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M")} for s, e in free_gaps(conn, d, now_local())]
        windows = _windows_json(get_day_windows(conn, d))
    return jsonify(date=d.isoformat(), label=day_label(d), appointments=result,
                   blocks=blocks, gaps=gaps, windows=windows)


@app.post("/api/admin/appointments")
@admin_required
def admin_add():
    """יובל מוסיפה תור בעצמה (למשל לקוחה שהתקשרה). טלפון לא חובה, אבל בלעדיו אין תזכורת בוואטסאפ."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if len(name) < 2:
        raise BookingError("צריך שם של לקוחה")
    raw_phone = (data.get("phone") or "").strip()
    phone = normalize_phone(raw_phone) if raw_phone else None
    if raw_phone and not phone:
        raise BookingError("מספר הטלפון לא נראה תקין (אפשר גם להשאיר ריק)")
    try:
        service_id = int(data["service_id"])
        start = datetime.fromisoformat(f"{data['date']}T{data['time']}")
    except (KeyError, ValueError, TypeError):
        raise BookingError("צריך לבחור טיפול, תאריך ושעה")
    _check_in_range(start.date())
    appt_id = book_appointment(name, service_id, start, customer_phone=phone, addon_count=data.get("addon") or 0)
    return jsonify(id=appt_id), 201


@app.post("/api/admin/appointments/<int:appt_id>/reminded")
@admin_required
def admin_mark_reminded(appt_id):
    with connection() as conn:
        cur = conn.execute(
            "UPDATE appointments SET reminder_sent_at = ? WHERE id = ? AND status = 'confirmed'",
            (now_local().strftime("%Y-%m-%d %H:%M"), appt_id),
        )
    if cur.rowcount == 0:
        return jsonify(error="התור לא נמצא"), 404
    return jsonify(ok=True)


@app.get("/api/admin/pending")
@admin_required
def admin_pending():
    """כל הבקשות שמחכות לאישור של יובל, מכל התאריכים."""
    with connection() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM appointments WHERE status = 'pending' AND start_time >= ? ORDER BY start_time",
            (now_local().strftime(TIME_FMT),))]
        rows = [_appointment_row(conn, i) for i in ids]
    return jsonify(pending=[{
        "id": r["id"], "name": r["customer_name"], "phone": r["customer_phone"], "service": r["service"],
        "date": r["start_time"][:10], "label": day_label(datetime.strptime(r["start_time"], TIME_FMT)),
        "start": r["start_time"][11:], "end": r["end_time"][11:],
    } for r in rows])


@app.post("/api/admin/appointments/<int:appt_id>/confirm")
@admin_required
def admin_confirm(appt_id):
    """יובל מאשרת בקשה. מחזיר קישור וואטסאפ עם הודעת אישור מוכנה ללקוחה."""
    confirm_appointment(appt_id)
    with connection() as conn:
        row = _appointment_row(conn, appt_id)
    return jsonify(ok=True, whatsapp_url=_wa_url(row["customer_phone"], approved_text(row)))


@app.post("/api/admin/appointments/<int:appt_id>/cancel")
@admin_required
def admin_cancel(appt_id):
    """ביטול תור, או דחיית בקשה שעוד לא אושרה. מחזיר קישור וואטסאפ עם הודעה מוכנה ללקוחה."""
    with connection() as conn:
        row = _appointment_row(conn, appt_id)
    if not row:
        return jsonify(error="התור לא נמצא"), 404
    was_pending = row["status"] == "pending"
    cancel_appointment(appt_id)
    if was_pending:
        text = declined_text(row)
    else:
        start = datetime.strptime(row["start_time"], TIME_FMT)
        text = (f"היי {row['customer_name'].split()[0]}, התור שלך ב{day_label(start)} בשעה "
                f"{start.strftime('%H:%M')} בוטל. אשמח לקבוע מועד אחר 🙏 יובל")
    return jsonify(ok=True, whatsapp_url=_wa_url(row["customer_phone"], text))


# ---------- ניהול שעות הפעילות ----------

WEEK_ORDER = [6, 0, 1, 2, 3, 4, 5]                 # ראשון עד שבת, במספור של פייתון
MAX_RANGE_DAYS = 60                                # כמה ימים אפשר לסגור/לאפס בפעולה אחת (חופש)


def _parse_time(value) -> str:
    try:
        return datetime.strptime(str(value), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise BookingError(f"שעה לא תקינה: {value}")


def _parse_windows(raw) -> list[tuple[str, str]]:
    """[{open, close}, ...] -> רשימה ממוינת, אחרי בדיקה שכל חלון תקין ושאין חפיפות."""
    if not isinstance(raw, list) or not raw:
        raise BookingError("צריך לפחות טווח שעות אחד")
    windows = sorted((_parse_time(w.get("open")), _parse_time(w.get("close"))) for w in raw)
    for open_t, close_t in windows:
        if open_t >= close_t:
            raise BookingError(f"שעת הסיום צריכה להיות אחרי שעת ההתחלה ({open_t}–{close_t})")
    for (_, close_a), (open_b, _) in zip(windows, windows[1:]):
        if open_b < close_a:
            raise BookingError("יש טווחי שעות שחופפים זה לזה")
    return windows


def _windows_json(windows):
    return [{"open": o.strftime("%H:%M"), "close": c.strftime("%H:%M")} for o, c in windows]


def _appointments_outside(conn, d: date) -> list[str]:
    """תורים קיימים שלא נכנסים בשעות החדשות של היום - כדי להזהיר את יובל (לא מבטלים לבד)."""
    windows = get_day_windows(conn, d)
    rows = conn.execute(
        "SELECT customer_name, start_time, end_time FROM appointments "
        f"WHERE {ACTIVE} AND date(start_time) = ? ORDER BY start_time", (d.isoformat(),)
    ).fetchall()
    outside = []
    for r in rows:
        start, end = datetime.strptime(r["start_time"], TIME_FMT), datetime.strptime(r["end_time"], TIME_FMT)
        if not any(o <= start and end <= c for o, c in windows):
            outside.append(f"{r['start_time'][11:]} {r['customer_name']}")
    return outside


@app.get("/api/admin/hours")
@admin_required
def admin_get_hours():
    with connection() as conn:
        rows = {r["weekday"]: r for r in conn.execute("SELECT * FROM working_hours")}
        breaks = {}
        for b in conn.execute("SELECT * FROM weekly_breaks ORDER BY start_time"):
            breaks.setdefault(b["weekday"], []).append({"start": b["start_time"], "end": b["end_time"]})
        until = last_bookable_day(conn)
    return jsonify(
        weekly=[
            {"weekday": wd, "name": HEB_WEEKDAYS[wd],
             "open": rows[wd]["open_time"] if wd in rows else None,
             "close": rows[wd]["close_time"] if wd in rows else None,
             "breaks": breaks.get(wd, [])}
            for wd in WEEK_ORDER
        ],
        booking_until=until.isoformat(),
        today=now_local().date().isoformat(),
        unset_days=unset_days_mode_now(),
    )


@app.post("/api/admin/hours")
@admin_required
def admin_set_hours():
    """השעות הקבועות לכל יום בשבוע. יום בלי שעות = סגור."""
    weekly = (request.get_json(silent=True) or {}).get("weekly")
    if not isinstance(weekly, list):
        raise BookingError("חסרות שעות")
    new_rows, new_breaks = [], []
    for item in weekly:
        wd = item.get("weekday")
        if wd not in range(7):
            raise BookingError("יום לא תקין")
        if item.get("closed") or not item.get("open"):    # יום בלי שעות = סגור
            continue
        (open_t, close_t), = _parse_windows([item])
        new_rows.append((wd, open_t, close_t))
        raw_breaks = item.get("breaks") or []
        if raw_breaks:
            name = HEB_WEEKDAYS[wd]
            breaks = _parse_windows([{"open": b.get("start"), "close": b.get("end")} for b in raw_breaks])
            for b_start, b_end in breaks:
                if b_start <= open_t or b_end >= close_t:
                    raise BookingError(f"ההפסקה ביום {name} ({b_start}–{b_end}) צריכה להיות בתוך שעות העבודה")
                new_breaks.append((wd, b_start, b_end))
    with connection() as conn:
        conn.execute("DELETE FROM working_hours")
        conn.execute("DELETE FROM weekly_breaks")
        conn.executemany("INSERT INTO working_hours (weekday, open_time, close_time) VALUES (?, ?, ?)", new_rows)
        conn.executemany("INSERT INTO weekly_breaks (weekday, start_time, end_time) VALUES (?, ?, ?)", new_breaks)
    return jsonify(ok=True)


@app.post("/api/admin/booking-until")
@admin_required
def admin_set_booking_until():
    """עד איזה תאריך לקוחות יכולות לקבוע תורים באתר."""
    try:
        until = date.fromisoformat((request.get_json(silent=True) or {}).get("date", ""))
    except ValueError:
        raise BookingError("תאריך לא תקין")
    if until < now_local().date():
        raise BookingError("התאריך כבר עבר")
    with connection() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('booking_until', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (until.isoformat(),))
    return jsonify(ok=True)


@app.get("/api/admin/day")
@admin_required
def admin_get_day():
    d = _date_arg()
    with connection() as conn:
        custom = conn.execute("SELECT 1 FROM day_hours WHERE date = ?", (d.isoformat(),)).fetchone() is not None
        windows = get_day_windows(conn, d)
        row = conn.execute("SELECT open_time, close_time FROM working_hours WHERE weekday = ?", (d.weekday(),)).fetchone()
        count = conn.execute(f"SELECT COUNT(*) FROM appointments WHERE {ACTIVE} AND date(start_time) = ?",
                             (d.isoformat(),)).fetchone()[0]
    return jsonify(
        date=d.isoformat(), label=day_label(d), custom=custom, windows=_windows_json(windows),
        regular=f"{row['open_time']}–{row['close_time']}" if row else None, appointments=count,
    )


@app.get("/api/admin/periods")
@admin_required
def admin_periods():
    """
    כל הימים המיוחדים מהיום והלאה, מקובצים לתקופות:
    ימים רצופים עם אותן שעות ואותה הערה מוצגים כשורה אחת ("חופש 12.10–16.10").
    """
    today = now_local().date()
    with connection() as conn:
        rows = conn.execute(
            "SELECT date, open_time, close_time, note FROM day_hours WHERE date >= ? ORDER BY date, open_time",
            (today.isoformat(),),
        ).fetchall()
    by_date = {}
    for r in rows:
        entry = by_date.setdefault(r["date"], {"windows": [], "note": r["note"]})
        if r["open_time"]:
            entry["windows"].append({"open": r["open_time"], "close": r["close_time"]})

    periods = []
    for iso in sorted(by_date):
        d, info = date.fromisoformat(iso), by_date[iso]
        last = periods[-1] if periods else None
        if (last and date.fromisoformat(last["until"]) + timedelta(days=1) == d
                and last["windows"] == info["windows"] and last["note"] == info["note"]):
            last["until"] = iso
            continue
        periods.append({"from": iso, "until": iso, "windows": info["windows"], "note": info["note"]})
    for p in periods:
        first, last = date.fromisoformat(p["from"]), date.fromisoformat(p["until"])
        p["label"] = day_label(first) if first == last else f"{day_label(first)} – {day_label(last)}"
        p["closed"] = not p["windows"]
    return jsonify(periods=periods)


@app.post("/api/admin/day")
@admin_required
def admin_set_day():
    """
    שעות לתאריך מסוים (או לטווח תאריכים, למשל חופש):
    mode = regular (חזרה לשעות הקבועות) · closed (סגור) · custom (שעות מיוחדות, אפשר כמה טווחים)
    """
    data = request.get_json(silent=True) or {}
    try:
        first = date.fromisoformat(data.get("date", ""))
        last = date.fromisoformat(data["until"]) if data.get("until") else first
    except ValueError:
        raise BookingError("תאריך לא תקין")
    if last < first:
        raise BookingError("תאריך הסיום לפני תאריך ההתחלה")
    if (last - first).days >= MAX_RANGE_DAYS:
        raise BookingError(f"אפשר לשנות עד {MAX_RANGE_DAYS} ימים בבת אחת")
    mode = data.get("mode")
    if mode not in ("regular", "closed", "custom"):
        raise BookingError("פעולה לא תקינה")
    windows = _parse_windows(data.get("windows")) if mode == "custom" else []
    note = (data.get("note") or "").strip()[:60] or None

    warnings = []
    with connection() as conn:
        d = first
        while d <= last:
            conn.execute("DELETE FROM day_hours WHERE date = ?", (d.isoformat(),))
            if mode == "closed":
                conn.execute("INSERT INTO day_hours (date, open_time, close_time, note) VALUES (?, NULL, NULL, ?)",
                             (d.isoformat(), note))
            elif mode == "custom":
                conn.executemany("INSERT INTO day_hours (date, open_time, close_time, note) VALUES (?, ?, ?, ?)",
                                 [(d.isoformat(), o, c, note) for o, c in windows])
            warnings += [f"{day_label(d)}: {x}" for x in _appointments_outside(conn, d)]
            d += timedelta(days=1)
    return jsonify(ok=True, outside=warnings)


# ---------- יומן זמינות ----------

def unset_days_mode_now() -> str:
    with connection() as conn:
        return unset_days_mode(conn)


@app.get("/api/admin/availability")
@admin_required
def admin_availability():
    """
    כל ימי החודש לעמוד "יומן זמינות": השעות של כל יום, האם יובל קבעה אותו בעצמה,
    וכמה תורים וחסימות יש בו. בנוסף: עד איזה יום הימים "מסודרים" ברצף מהיום.
    """
    try:
        year, mon = map(int, request.args.get("month", "").split("-"))
        first = date(year, mon, 1)
    except ValueError:
        raise BookingError("חודש לא תקין")
    next_month = date(year + (mon == 12), mon % 12 + 1, 1)
    today = now_local().date()
    with connection() as conn:
        mode = unset_days_mode(conn)
        set_dates = {r["date"] for r in conn.execute("SELECT DISTINCT date FROM day_hours")}
        counts = {r["d"]: r["n"] for r in conn.execute(
            "SELECT date(start_time) AS d, COUNT(*) AS n FROM appointments "
            f"WHERE {ACTIVE} "
            "AND start_time >= ? AND start_time < ? GROUP BY d", (first.isoformat(), next_month.isoformat()))}
        blocks = {r["date"]: r["n"] for r in conn.execute(
            "SELECT date, COUNT(*) AS n FROM blocks WHERE date >= ? AND date < ? GROUP BY date",
            (first.isoformat(), next_month.isoformat()))}
        days, d = [], first
        while d < next_month:
            iso = d.isoformat()
            windows = get_day_windows(conn, d)
            days.append({
                "date": iso, "past": d < today, "set": iso in set_dates,
                "windows": _windows_json(windows), "appointments": counts.get(iso, 0), "blocks": blocks.get(iso, 0),
            })
            d += timedelta(days=1)

        # עד מתי הימים מסודרים ברצף (רלוונטי רק כשיום שלא נקבע = סגור)
        open_until = None
        if mode == "closed":
            d = today
            while d.isoformat() in set_dates and (d - today).days < 400:
                d += timedelta(days=1)
            open_until = (d - timedelta(days=1)).isoformat() if d > today else None
    return jsonify(month=first.strftime("%Y-%m"), days=days, mode=mode, today=today.isoformat(),
                   open_until=open_until, open_until_label=day_label(date.fromisoformat(open_until)) if open_until else None)


@app.post("/api/admin/copy-week")
@admin_required
def admin_copy_week():
    """מעתיק את השעות של 7 ימים (מ-from) ל-7 הימים שמתחילים ב-to."""
    data = request.get_json(silent=True) or {}
    try:
        src, dst = date.fromisoformat(data.get("from", "")), date.fromisoformat(data.get("to", ""))
    except ValueError:
        raise BookingError("תאריך לא תקין")
    if abs((dst - src).days) < 7:
        raise BookingError("השבועות חופפים")
    warnings = []
    with connection() as conn:
        for i in range(7):
            s, t = (src + timedelta(days=i)).isoformat(), dst + timedelta(days=i)
            rows = conn.execute("SELECT open_time, close_time, note FROM day_hours WHERE date = ?", (s,)).fetchall()
            conn.execute("DELETE FROM day_hours WHERE date = ?", (t.isoformat(),))
            conn.executemany("INSERT INTO day_hours (date, open_time, close_time, note) VALUES (?, ?, ?, ?)",
                             [(t.isoformat(), r["open_time"], r["close_time"], r["note"]) for r in rows])
            warnings += [f"{day_label(t)}: {x}" for x in _appointments_outside(conn, t)]
    return jsonify(ok=True, outside=warnings)


@app.post("/api/admin/settings")
@admin_required
def admin_settings():
    mode = (request.get_json(silent=True) or {}).get("unset_days")
    if mode not in ("closed", "weekly"):
        raise BookingError("הגדרה לא תקינה")
    with connection() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('unset_days', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (mode,))
    return jsonify(ok=True)


# ---------- חסימת זמן ----------

@app.post("/api/admin/blocks")
@admin_required
def admin_add_block():
    data = request.get_json(silent=True) or {}
    try:
        d = date.fromisoformat(data.get("date", ""))
    except ValueError:
        raise BookingError("תאריך לא תקין")
    (start, end), = _parse_windows([{"open": data.get("start"), "close": data.get("end")}])
    note = (data.get("note") or "").strip()[:80] or None
    s_dt, e_dt = datetime.fromisoformat(f"{d}T{start}"), datetime.fromisoformat(f"{d}T{end}")
    with connection() as conn:
        for r in conn.execute("SELECT customer_name, start_time, end_time FROM appointments "
                              f"WHERE {ACTIVE} AND date(start_time) = ?", (d.isoformat(),)):
            a_s, a_e = datetime.strptime(r["start_time"], TIME_FMT), datetime.strptime(r["end_time"], TIME_FMT)
            if s_dt < a_e and a_s < e_dt:
                raise BookingError(f"יש באותו זמן תור של {r['customer_name']} ({r['start_time'][11:]})")
        cur = conn.execute("INSERT INTO blocks (date, start_time, end_time, note) VALUES (?, ?, ?, ?)",
                           (d.isoformat(), start, end, note))
    return jsonify(id=cur.lastrowid), 201


@app.post("/api/admin/blocks/<int:block_id>/delete")
@admin_required
def admin_delete_block(block_id):
    with connection() as conn:
        cur = conn.execute("DELETE FROM blocks WHERE id = ?", (block_id,))
    if cur.rowcount == 0:
        return jsonify(error="החסימה לא נמצאה"), 404
    return jsonify(ok=True)


# ---------- יומן (קבצי .ics) ----------
# .ics הוא פורמט סטנדרטי שכל אפליקציית יומן מבינה. שומרים זמנים ב-UTC (עם Z בסוף),
# ככה הטלפון ממיר לשעון המקומי בלי בלבול של שעון קיץ/חורף.

def _ics_time(text: str) -> str:
    local = datetime.strptime(text, TIME_FMT).replace(tzinfo=BUSINESS_TZ)
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_event(uid, start, end, summary, description="", location="", alarm=None) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{datetime.now(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')}",
             f"DTSTART:{_ics_time(start)}", f"DTEND:{_ics_time(end)}", f"SUMMARY:{_ics_escape(summary)}"]
    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    if alarm:
        lines += ["BEGIN:VALARM", f"TRIGGER:{alarm}", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(summary)}", "END:VALARM"]
    return lines + ["END:VEVENT"]


def _ics_response(events: list[str], name: str, filename: str, refresh=False) -> Response:
    head = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//yuvalnails//booking//HE", "CALSCALE:GREGORIAN",
            f"X-WR-CALNAME:{_ics_escape(name)}"]
    if refresh:                                      # בקשה לאייפון לבדוק עדכונים כל 15 דקות
        head += ["REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M"]
    body = "\r\n".join(head + events + ["END:VCALENDAR"]) + "\r\n"
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-cache"})


def calendar_token(conn) -> str:
    """הקוד הסודי של יומן המנוי של יובל. נוצר פעם אחת ונשמר."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'calendar_token'").fetchone()
    if row:
        return row["value"]
    token = secrets.token_urlsafe(18)
    conn.execute("INSERT INTO settings (key, value) VALUES ('calendar_token', ?)", (token,))
    return token


@app.get("/calendar/<token>.ics")
def owner_calendar(token):
    """
    יומן המנוי של יובל: כל התורים שאושרו, מחודש אחורה והלאה.
    האייפון חוזר לכתובת הזו לבד ומתעדכן - תור חדש מופיע, תור שבוטל נעלם.
    """
    with connection() as conn:
        if not hmac.compare_digest(token.encode(), calendar_token(conn).encode()):
            return jsonify(error="לא נמצא"), 404
        since = (now_local() - timedelta(days=31)).strftime(TIME_FMT)
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM appointments WHERE status = 'confirmed' AND start_time >= ? ORDER BY start_time", (since,))]
        rows = [_appointment_row(conn, i) for i in ids]
    events = []
    for r in rows:
        desc = "\n".join(x for x in [r["service"], r["customer_phone"] or ""] if x)
        events += _ics_event(f"yuvalnails-appt-{r['id']}@booking", r["start_time"], r["end_time"],
                             f"💅 {r['customer_name']} · {r['service']}", description=desc)
    return _ics_response(events, "תורים - yuvalnails", "yuvalnails.ics", refresh=True)


@app.get("/booking/<tokens>.ics")
def customer_calendar(tokens):
    """קובץ יומן ללקוחה - רק לתורים שיובל כבר אישרה. תומך בכמה תורים (התור + התור הבא)."""
    address = getattr(config, "STUDIO_ADDRESS", None) or ""
    events = []
    with connection() as conn:
        for t in tokens.split(",")[:4]:
            row = conn.execute("SELECT id FROM appointments WHERE public_token = ? AND status = 'confirmed'",
                               (t,)).fetchone()
            if row:
                r = _appointment_row(conn, row["id"])
                events += _ics_event(f"yuvalnails-{t}@booking", r["start_time"], r["end_time"],
                                     f"{r['service']} אצל יובל 💅", location=address, alarm="-P1D")
    if not events:
        return jsonify(error="התור לא נמצא או עוד לא אושר"), 404
    return _ics_response(events, "yuvalnails", "yuvalnails.ics")


@app.get("/api/admin/calendar")
@admin_required
def admin_calendar_link():
    with connection() as conn:
        token = calendar_token(conn)
    url = f"{public_root()}calendar/{token}.ics"
    return jsonify(url=url, webcal=re.sub(r"^https?://", "webcal://", url))


if __name__ == "__main__":
    init_db()
    print("האתר רץ: http://localhost:5000   ניהול: http://localhost:5000/admin   (לעצירה: Ctrl+C)")
    app.run(port=5000, debug=False)
