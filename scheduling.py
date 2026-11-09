"""
לוגיקת התורים: חישוב שעות פנויות וקביעת תור.

הרעיון המרכזי: תור חדש [start, end) מתנגש עם תור קיים [s, e)
אם ורק אם  start < e  וגם  s < end.
זו הבדיקה היחידה שצריך - היא מכסה חפיפה חלקית, הכלה, וזהות.
"""

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from db import connection

SLOT_STEP_MINUTES = 15   # היום הנוכחי: לא מציעים תור שמתחיל לפני הרבע-שעה העגולה הבאה
TIME_FMT = "%Y-%m-%d %H:%M"
BUSINESS_TZ = ZoneInfo("Asia/Jerusalem")


class BookingError(Exception):
    pass


def now_local() -> datetime:
    """
    השעה עכשיו בישראל. חשוב כשהשרת ירוץ בענן: שרתים בדרך כלל מכוונים ל-UTC,
    ואז datetime.now() "רגיל" היה מציע שעות שכבר עברו (או מסתיר שעות פנויות).
    """
    return datetime.now(BUSINESS_TZ).replace(tzinfo=None)


def _get_service(conn, service_id):
    row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    if row is None:
        raise BookingError(f"שירות {service_id} לא קיים")
    return row


def get_addon(conn=None):
    """התוספת (השלמת ציפורן), או None אם אין."""
    if conn is None:
        with connection() as conn:
            return get_addon(conn)
    return conn.execute("SELECT * FROM services WHERE is_addon = 1 LIMIT 1").fetchone()


MAX_ADDON = 10      # הכי הרבה השלמות ציפורן בתור אחד


def _addon_count(value) -> int:
    """True/False (מהגרסה הקודמת) או מספר -> מספר בין 0 ל-MAX_ADDON."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise BookingError("מספר השלמות לא תקין")
    if not 0 <= count <= MAX_ADDON:
        raise BookingError(f"אפשר להוסיף עד {MAX_ADDON} השלמות ציפורן")
    return count


def _duration(conn, service_id, addon_count):
    service = _get_service(conn, service_id)
    if service["is_addon"]:
        raise BookingError("השלמת ציפורן היא תוספת, צריך לבחור טיפול")
    minutes = service["duration_minutes"]
    count = _addon_count(addon_count)
    if count and not service["allows_addon"]:
        raise BookingError("לטיפול הזה אי אפשר להוסיף השלמת ציפורן")
    if count:
        addon = get_addon(conn)
        minutes += addon["duration_minutes"] * count if addon else 0
    return timedelta(minutes=minutes)


def _to_dt(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())


def unset_days_mode(conn) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = 'unset_days'").fetchone()
    return row["value"] if row and row["value"] in ("closed", "weekly") else "closed"


def get_day_windows(conn, day: date):
    """
    חלונות העבודה של יום מסוים, כרשימה של (פתיחה, סגירה). רשימה ריקה = סגור.

    סדר העדיפויות:
    1. אם יובל קבעה שעות לתאריך הזה (טבלת day_hours) - רק הן קובעות.
       כך היא יכולה לסגור יום (חופש), לפתוח יום שבדרך כלל סגור, או לעשות הפסקה
       (שני חלונות: 09:00-13:00 ו-16:00-20:00).
    2. אחרת, תלוי בהגדרה unset_days:
       closed (ברירת מחדל) - היום סגור עד שיובל פותחת אותו ביומן הזמינות;
       weekly - השעות הקבועות של אותו יום בשבוע (טבלת working_hours), פחות ההפסקות הקבועות.
    """
    rows = conn.execute(
        "SELECT open_time, close_time FROM day_hours WHERE date = ? ORDER BY open_time", (day.isoformat(),)
    ).fetchall()
    if rows:
        return [(_to_dt(day, r["open_time"]), _to_dt(day, r["close_time"])) for r in rows if r["open_time"]]
    if unset_days_mode(conn) == "closed":
        return []                                    # יובל עוד לא פתחה את היום הזה
    row = conn.execute(
        "SELECT open_time, close_time FROM working_hours WHERE weekday = ?", (day.weekday(),)
    ).fetchone()
    if row is None:
        return []
    # השעות הקבועות, "חתוכות" בהפסקות הקבועות של אותו יום בשבוע
    windows = [(_to_dt(day, row["open_time"]), _to_dt(day, row["close_time"]))]
    breaks = conn.execute(
        "SELECT start_time, end_time FROM weekly_breaks WHERE weekday = ? ORDER BY start_time", (day.weekday(),)
    ).fetchall()
    for b in breaks:
        b_start, b_end = _to_dt(day, b["start_time"]), _to_dt(day, b["end_time"])
        cut = []
        for w_open, w_close in windows:
            if b_end <= w_open or w_close <= b_start:     # ההפסקה לא נוגעת בחלון הזה
                cut.append((w_open, w_close))
                continue
            if w_open < b_start:
                cut.append((w_open, b_start))
            if b_end < w_close:
                cut.append((b_end, w_close))
        windows = cut
    return windows


# תור "פעיל" = תופס זמן ביומן: גם תור שאושר וגם בקשה שמחכה לאישור של יובל
ACTIVE = "status IN ('pending', 'confirmed')"


def _get_busy_ranges(conn, day: date):
    """כל הזמן התפוס ביום: תורים פעילים וחסימות של יובל, כרשימה של (התחלה, סוף)."""
    rows = conn.execute(
        "SELECT start_time, end_time FROM appointments "
        f"WHERE {ACTIVE} AND date(start_time) = ?",
        (day.isoformat(),),
    ).fetchall()
    busy = [(datetime.strptime(r["start_time"], TIME_FMT), datetime.strptime(r["end_time"], TIME_FMT)) for r in rows]
    blocks = conn.execute("SELECT start_time, end_time FROM blocks WHERE date = ?", (day.isoformat(),)).fetchall()
    busy += [(_to_dt(day, b["start_time"]), _to_dt(day, b["end_time"])) for b in blocks]
    return busy


def _overlaps(start, end, busy_ranges):
    return any(start < b_end and b_start < end for b_start, b_end in busy_ranges)


def get_available_slots(service_id, day: date, now: datetime | None = None, conn=None, addon_count=0):
    """
    מחזיר רשימת שעות התחלה אפשריות לשירות ביום מסוים.

    now - "השעה הנוכחית", כדי לא להציע שעות שכבר עברו.
          מקבלים אותו כפרמטר (ולא קוראים לשעון בפנים) כדי שיהיה קל לבדוק.
    addon_count - כמה השלמות ציפורן הלקוחה הוסיפה; כל אחת מאריכה את התור.
    """
    if conn is None:
        with connection() as conn:
            return get_available_slots(service_id, day, now=now, conn=conn, addon_count=addon_count)
    now = now or now_local()
    duration = _duration(conn, service_id, addon_count)

    slots = []
    for gap_start, gap_end in free_gaps(conn, day, now):
        # "תורים בשרשרת": מתחילים בתחילת הזמן הפנוי, וכל תור הבא מתחיל בדיוק כשהקודם נגמר.
        # ככה לא נוצרים "חורים" קטנים ביומן של יובל (למשל רבע שעה מבוזבזת בין שני תורים).
        candidate = gap_start
        while candidate + duration <= gap_end:
            slots.append(candidate)
            candidate += duration
    return slots


def _round_up(dt: datetime, minutes=SLOT_STEP_MINUTES) -> datetime:
    """14:07 -> 14:15. כדי שהיום לא יציע תור שמתחיל בשעה "עקומה" כמו 14:07."""
    dt = dt.replace(second=0, microsecond=0)
    extra = -dt.minute % minutes
    return dt + timedelta(minutes=extra)


def free_gaps(conn, day: date, now: datetime):
    """
    הזמנים הפנויים ביום, כרשימה של (התחלה, סוף):
    חלונות העבודה, פחות התורים שכבר נקבעו, פחות מה שכבר עבר.
    """
    busy = sorted(_get_busy_ranges(conn, day))
    gaps = []
    for w_open, w_close in get_day_windows(conn, day):
        cursor = max(w_open, _round_up(now)) if now > w_open else w_open
        for b_start, b_end in busy:
            if b_end <= cursor or b_start >= w_close:
                continue
            if b_start > cursor:
                gaps.append((cursor, min(b_start, w_close)))
            cursor = max(cursor, b_end)
        if cursor < w_close:
            gaps.append((cursor, w_close))
    return gaps


def book_appointment(customer_name, service_id, start: datetime, customer_chat=None,
                     now: datetime | None = None, conn=None, customer_phone=None, addon_count=0,
                     status="confirmed", public_token=None):
    """
    status: confirmed = תור קבוע (למשל יובל הוסיפה בעצמה), pending = בקשה מהאתר שמחכה לאישור.
    קובע תור ומחזיר את המזהה שלו. בודק שוב זמינות לפני השמירה - כי בין הרגע
    שהלקוחה ראתה את השעות הפנויות לרגע שלחצה, מישהי אחרת אולי כבר תפסה את השעה.
    """
    if conn is None:
        with connection() as conn:
            return book_appointment(customer_name, service_id, start, customer_chat, now, conn,
                                    customer_phone, addon_count, status, public_token)
    if start not in get_available_slots(service_id, start.date(), now=now, conn=conn, addon_count=addon_count):
        raise BookingError("השעה הזו כבר לא פנויה")

    end = start + _duration(conn, service_id, addon_count)
    with conn:
        cur = conn.execute(
            "INSERT INTO appointments "
            "(customer_name, customer_phone, customer_chat, service_id, with_addon, start_time, end_time, "
            "status, public_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (customer_name, customer_phone, customer_chat, service_id, _addon_count(addon_count),
             start.strftime(TIME_FMT), end.strftime(TIME_FMT), status, public_token),
        )
    return cur.lastrowid


def confirm_appointment(appointment_id, conn=None):
    """יובל מאשרת בקשה לתור: pending -> confirmed."""
    if conn is None:
        with connection() as conn:
            return confirm_appointment(appointment_id, conn)
    with conn:
        cur = conn.execute("UPDATE appointments SET status = 'confirmed' WHERE id = ? AND status = 'pending'",
                           (appointment_id,))
    if cur.rowcount == 0:
        raise BookingError("הבקשה לא נמצאה או שכבר טופלה")


def cancel_appointment(appointment_id, conn=None, deposit_status=None):
    """ביטול = שינוי סטטוס, לא מחיקה. ככה נשמרת היסטוריה (שימושי לתזכורות מילוי ולדוחות)."""
    if conn is None:
        with connection() as conn:
            return cancel_appointment(appointment_id, conn, deposit_status)
    with conn:
        # deposit_status: מה קרה למקדמה בביטול (refunded = הוחזרה, kept = נשארה). None = לא משנים
        cur = conn.execute(
            "UPDATE appointments SET status = 'cancelled', cancelled_at = ?, "
            f"deposit_status = COALESCE(?, deposit_status) WHERE id = ? AND {ACTIVE}",
            (now_local().strftime(TIME_FMT), deposit_status, appointment_id),
        )
    if cur.rowcount == 0:
        raise BookingError(f"תור {appointment_id} לא נמצא או כבר בוטל")
