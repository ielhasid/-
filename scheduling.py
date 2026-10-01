"""
לוגיקת התורים: חישוב שעות פנויות וקביעת תור.

הרעיון המרכזי: תור חדש [start, end) מתנגש עם תור קיים [s, e)
אם ורק אם  start < e  וגם  s < end.
זו הבדיקה היחידה שצריך - היא מכסה חפיפה חלקית, הכלה, וזהות.
"""

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from db import connection

SLOT_STEP_MINUTES = 15   # כל כמה דקות מציעים שעת התחלה (09:00, 09:15, 09:30...)
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


def _duration(conn, service_id, with_addon):
    service = _get_service(conn, service_id)
    if service["is_addon"]:
        raise BookingError("השלמת ציפורן היא תוספת, צריך לבחור טיפול")
    minutes = service["duration_minutes"]
    if with_addon:
        addon = get_addon(conn)
        minutes += addon["duration_minutes"] if addon else 0
    return timedelta(minutes=minutes)


def _get_working_hours(conn, day: date):
    """מחזיר (פתיחה, סגירה) כ-datetime, או None אם העסק סגור ביום הזה."""
    row = conn.execute(
        "SELECT open_time, close_time FROM working_hours WHERE weekday = ?",
        (day.weekday(),),
    ).fetchone()
    if row is None:
        return None
    open_dt = datetime.combine(day, datetime.strptime(row["open_time"], "%H:%M").time())
    close_dt = datetime.combine(day, datetime.strptime(row["close_time"], "%H:%M").time())
    return open_dt, close_dt


def _get_busy_ranges(conn, day: date):
    """כל התורים הפעילים ביום מסוים, כרשימה של (התחלה, סוף)."""
    rows = conn.execute(
        "SELECT start_time, end_time FROM appointments "
        "WHERE status = 'confirmed' AND date(start_time) = ?",
        (day.isoformat(),),
    ).fetchall()
    return [
        (datetime.strptime(r["start_time"], TIME_FMT), datetime.strptime(r["end_time"], TIME_FMT))
        for r in rows
    ]


def _overlaps(start, end, busy_ranges):
    return any(start < b_end and b_start < end for b_start, b_end in busy_ranges)


def get_available_slots(service_id, day: date, now: datetime | None = None, conn=None, with_addon=False):
    """
    מחזיר רשימת שעות התחלה אפשריות לשירות ביום מסוים.

    now - "השעה הנוכחית", כדי לא להציע שעות שכבר עברו.
          מקבלים אותו כפרמטר (ולא קוראים לשעון בפנים) כדי שיהיה קל לבדוק.
    with_addon - אם הלקוחה הוסיפה השלמת ציפורן, התור ארוך יותר.
    """
    if conn is None:
        with connection() as conn:
            return get_available_slots(service_id, day, now=now, conn=conn, with_addon=with_addon)
    now = now or now_local()
    duration = _duration(conn, service_id, with_addon)

    hours = _get_working_hours(conn, day)
    if hours is None:
        return []
    open_dt, close_dt = hours

    busy = _get_busy_ranges(conn, day)
    slots = []
    candidate = open_dt
    while candidate + duration <= close_dt:          # השירות חייב להסתיים עד הסגירה
        if candidate > now and not _overlaps(candidate, candidate + duration, busy):
            slots.append(candidate)
        candidate += timedelta(minutes=SLOT_STEP_MINUTES)
    return slots


def book_appointment(customer_name, service_id, start: datetime, customer_chat=None,
                     now: datetime | None = None, conn=None, customer_phone=None, with_addon=False):
    """
    קובע תור ומחזיר את המזהה שלו. בודק שוב זמינות לפני השמירה - כי בין הרגע
    שהלקוחה ראתה את השעות הפנויות לרגע שלחצה, מישהי אחרת אולי כבר תפסה את השעה.
    """
    if conn is None:
        with connection() as conn:
            return book_appointment(customer_name, service_id, start, customer_chat, now, conn,
                                    customer_phone, with_addon)
    if start not in get_available_slots(service_id, start.date(), now=now, conn=conn, with_addon=with_addon):
        raise BookingError("השעה הזו כבר לא פנויה")

    end = start + _duration(conn, service_id, with_addon)
    with conn:
        cur = conn.execute(
            "INSERT INTO appointments "
            "(customer_name, customer_phone, customer_chat, service_id, with_addon, start_time, end_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (customer_name, customer_phone, customer_chat, service_id, int(with_addon),
             start.strftime(TIME_FMT), end.strftime(TIME_FMT)),
        )
    return cur.lastrowid


def cancel_appointment(appointment_id, conn=None):
    """ביטול = שינוי סטטוס, לא מחיקה. ככה נשמרת היסטוריה (שימושי לתזכורות מילוי ולדוחות)."""
    if conn is None:
        with connection() as conn:
            return cancel_appointment(appointment_id, conn)
    with conn:
        cur = conn.execute(
            "UPDATE appointments SET status = 'cancelled' WHERE id = ? AND status = 'confirmed'",
            (appointment_id,),
        )
    if cur.rowcount == 0:
        raise BookingError(f"תור {appointment_id} לא נמצא או כבר בוטל")
