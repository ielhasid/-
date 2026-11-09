"""
בדיקות לוגיקת התורים. הרצה:  python -m unittest test_scheduling.py -v

כל בדיקה רצה על מסד נתונים בזיכרון (":memory:") - נקי, מהיר, ולא נוגע בקובץ האמיתי.
"""

import unittest
from datetime import datetime, date

from db import get_connection, setup_schema
from scheduling import get_available_slots, book_appointment, cancel_appointment, BookingError

SUNDAY = date(2026, 10, 4)
FRIDAY = date(2026, 10, 2)
SATURDAY = date(2026, 10, 3)
NOW = datetime(2026, 10, 1, 12, 0)   # "עכשיו" קבוע לבדיקות - יום לפני כל התאריכים

GEL_60 = 2      # לק ג'ל ללא מבנה, 60 דק'
BUILD_90 = 4    # בנייה מלאה, 90 דק'
ADDON = 5       # השלמת ציפורן (תוספת, 10 דק')


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        setup_schema(self.conn)
        # רוב הבדיקות כאן בודקות לוגיקה מעל השעות הקבועות
        self.conn.execute("UPDATE settings SET value = 'weekly' WHERE key = 'unset_days'")

    def slots(self, service, day, now=NOW):
        return [s.strftime("%H:%M") for s in get_available_slots(service, day, now=now, conn=self.conn)]

    def test_empty_day_first_and_last_slot(self):
        s = self.slots(GEL_60, SUNDAY)
        self.assertEqual(s[0], "09:00")
        self.assertEqual(s[-1], "18:00")   # 60 דק' חייבות להסתיים עד 19:00

    def test_closed_on_saturday(self):
        self.assertEqual(self.slots(GEL_60, SATURDAY), [])

    def test_friday_short_day(self):
        self.assertEqual(self.slots(BUILD_90, FRIDAY), ["09:00", "10:30", "12:00"])   # שרשרת של 90 דק' עד 14:00

    def test_empty_day_is_a_chain(self):
        # בלי תורים: 60 דק' -> כל שעה עגולה; 30 דק' -> כל חצי שעה
        self.assertEqual(self.slots(GEL_60, SUNDAY)[:3], ["09:00", "10:00", "11:00"])
        self.assertEqual(self.slots(6, SUNDAY)[:3], ["09:00", "09:30", "10:00"])

    def test_next_slot_starts_exactly_when_previous_ends(self):
        book_appointment("נועה", BUILD_90, datetime(2026, 10, 4, 9, 0), now=NOW, conn=self.conn)  # 09:00-10:30
        s = self.slots(GEL_60, SUNDAY)
        self.assertEqual(s[0], "10:30")                 # בדיוק כשהבנייה נגמרת
        self.assertNotIn("10:45", s)                    # לא משאירים חור של רבע שעה
        self.assertEqual(s[1], "11:30")                 # וממשיכים בשרשרת
        self.assertEqual(s[-1], "17:30")                # עד סוף היום (18:30 כבר לא נכנס עד 19:00)

    def test_gap_between_two_appointments(self):
        book_appointment("נועה", GEL_60, datetime(2026, 10, 4, 9, 0), now=NOW, conn=self.conn)    # 09:00-10:00
        # תור נוסף ב-11:00 משאיר חלון של שעה בין 10:00 ל-11:00
        book_appointment("מאיה", GEL_60, datetime(2026, 10, 4, 11, 0), now=NOW, conn=self.conn)
        s = self.slots(6, SUNDAY)                       # 30 דק'
        self.assertEqual(s[:3], ["10:00", "10:30", "12:00"])

    def test_cannot_double_book(self):
        start = datetime(2026, 10, 4, 10, 0)
        book_appointment("נועה", GEL_60, start, now=NOW, conn=self.conn)
        with self.assertRaises(BookingError):
            book_appointment("מאיה", GEL_60, start, now=NOW, conn=self.conn)

    def test_past_slots_not_offered(self):
        s = self.slots(GEL_60, SUNDAY, now=datetime(2026, 10, 4, 14, 10))
        self.assertEqual(s[0], "14:15")

    def test_cancel_frees_slot(self):
        start = datetime(2026, 10, 4, 10, 0)
        appt = book_appointment("נועה", GEL_60, start, now=NOW, conn=self.conn)
        cancel_appointment(appt, conn=self.conn)
        self.assertIn("10:00", self.slots(GEL_60, SUNDAY))

    def test_addon_does_not_change_slots(self):
        # השלמת ציפורן נכללת בזמן הטיפול: אותן שעות בדיוק כמו בלי השלמה
        s = [t.strftime("%H:%M") for t in get_available_slots(GEL_60, SUNDAY, now=NOW, conn=self.conn, addon_count=1)]
        self.assertEqual(s[:2], ["09:00", "10:00"])    # השלמה לא מאריכה את התור (נכללת בזמן)
        self.assertEqual(s[-1], "18:00")

    def test_several_addons_still_one_hour(self):
        s = [t.strftime("%H:%M") for t in get_available_slots(GEL_60, SUNDAY, now=NOW, conn=self.conn, addon_count=3)]
        self.assertEqual(s[-1], "18:00")
        appt = book_appointment("נועה", GEL_60, datetime(2026, 10, 4, 9, 0), now=NOW, conn=self.conn,
                                customer_phone="0501234567", addon_count=3)
        row = self.conn.execute("SELECT with_addon, end_time FROM appointments WHERE id = ?", (appt,)).fetchone()
        self.assertEqual((row["with_addon"], row["end_time"]), (3, "2026-10-04 10:00"))

    def test_addon_count_limits(self):
        for bad in (-1, 11, "abc"):
            with self.assertRaises(BookingError, msg=bad):
                get_available_slots(GEL_60, SUNDAY, now=NOW, conn=self.conn, addon_count=bad)

    def test_addon_cannot_be_booked_alone(self):
        with self.assertRaises(BookingError):
            book_appointment("נועה", ADDON, datetime(2026, 10, 4, 10, 0), now=NOW, conn=self.conn)

    def test_phone_and_addon_are_saved(self):
        appt = book_appointment("נועה", GEL_60, datetime(2026, 10, 4, 9, 0), now=NOW, conn=self.conn,
                                customer_phone="0501234567", addon_count=1)
        row = self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appt,)).fetchone()
        self.assertEqual(row["customer_phone"], "0501234567")
        self.assertEqual(row["end_time"], "2026-10-04 10:00")

    def test_changed_durations_update_existing_database(self):
        self.conn.execute("UPDATE services SET duration_minutes = 999, price = 1 WHERE id = 4")
        setup_schema(self.conn)
        row = self.conn.execute("SELECT duration_minutes, price FROM services WHERE id = 4").fetchone()
        self.assertEqual((row["duration_minutes"], row["price"]), (90, 250))

    # --- שעות שיובל קובעת ---

    def set_day(self, day, *windows):
        self.conn.execute("DELETE FROM day_hours WHERE date = ?", (day.isoformat(),))
        rows = windows or [(None, None)]
        self.conn.executemany("INSERT INTO day_hours (date, open_time, close_time) VALUES (?, ?, ?)", [(day.isoformat(), o, c) for o, c in rows])

    def test_closed_date_overrides_weekly_hours(self):
        self.set_day(SUNDAY)                                     # חופש
        self.assertEqual(self.slots(GEL_60, SUNDAY), [])

    def test_open_a_normally_closed_saturday(self):
        self.set_day(SATURDAY, ("18:00", "21:00"))              # מוצאי שבת
        s = self.slots(GEL_60, SATURDAY)
        self.assertEqual((s[0], s[-1]), ("18:00", "20:00"))

    def test_break_in_the_middle_of_the_day(self):
        self.set_day(SUNDAY, ("09:00", "12:00"), ("15:00", "18:00"))
        s = self.slots(GEL_60, SUNDAY)
        self.assertIn("11:00", s)                                # 11:00-12:00 נכנס לפני ההפסקה
        self.assertNotIn("11:15", s)                             # היה נגמר בתוך ההפסקה
        self.assertNotIn("13:00", s)
        self.assertEqual(s[-1], "17:00")

    def test_weekly_hours_without_override(self):
        self.conn.execute("UPDATE working_hours SET open_time = '12:00', close_time = '16:00' WHERE weekday = 6")
        s = self.slots(GEL_60, SUNDAY)
        self.assertEqual((s[0], s[-1]), ("12:00", "15:00"))

    def test_weekly_break_splits_the_day(self):
        self.conn.execute("INSERT INTO weekly_breaks VALUES (6, '13:00', '14:00')")   # הפסקה כל יום ראשון
        s = self.slots(GEL_60, SUNDAY)
        self.assertIn("12:00", s)                       # 12:00-13:00 נגמר בדיוק בתחילת ההפסקה
        self.assertNotIn("13:00", s)
        self.assertIn("14:00", s)                       # חוזרים בדיוק בסוף ההפסקה
        self.assertEqual(self.slots(BUILD_90, SUNDAY)[:3], ["09:00", "10:30", "14:00"])   # 12:00-13:30 לא נכנס

    def test_date_override_ignores_weekly_breaks(self):
        self.conn.execute("INSERT INTO weekly_breaks VALUES (6, '13:00', '14:00')")
        self.set_day(SUNDAY, ("12:00", "16:00"))         # ביום הזה יובל קבעה שעות מיוחדות בלי הפסקה
        self.assertEqual(self.slots(GEL_60, SUNDAY), ["12:00", "13:00", "14:00", "15:00"])

    def test_two_breaks(self):
        self.conn.executemany("INSERT INTO weekly_breaks VALUES (6, ?, ?)", [("11:00", "11:30"), ("15:00", "16:00")])
        s = self.slots(6, SUNDAY)                        # 30 דק'
        self.assertNotIn("11:00", s)
        self.assertIn("11:30", s)
        self.assertNotIn("15:30", s)
        self.assertIn("16:00", s)

    def test_unset_day_is_closed_by_default(self):
        self.conn.execute("UPDATE settings SET value = 'closed' WHERE key = 'unset_days'")
        self.assertEqual(self.slots(GEL_60, SUNDAY), [])          # יובל עוד לא פתחה את היום
        self.set_day(SUNDAY, ("09:00", "14:00"))                   # "בוקר"
        self.assertEqual(self.slots(GEL_60, SUNDAY), ["09:00", "10:00", "11:00", "12:00", "13:00"])

    def test_block_takes_time_like_an_appointment(self):
        self.conn.execute("INSERT INTO blocks (date, start_time, end_time, note) VALUES ('2026-10-04', '10:00', '11:30', 'וואטסאפ')")
        s = self.slots(GEL_60, SUNDAY)
        self.assertEqual(s[:3], ["09:00", "11:30", "12:30"])       # השרשרת ממשיכה בדיוק אחרי החסימה

    def test_new_database_defaults_to_closed(self):
        fresh = get_connection(":memory:")
        setup_schema(fresh)
        self.assertEqual(fresh.execute("SELECT value FROM settings WHERE key = 'unset_days'").fetchone()[0], "closed")

    def test_weekly_hours_survive_restart(self):
        self.conn.execute("DELETE FROM working_hours WHERE weekday = 6")   # יובל סגרה את ימי ראשון
        setup_schema(self.conn)                                           # השרת עלה מחדש
        self.assertEqual(self.slots(GEL_60, SUNDAY), [])

    def test_old_database_is_upgraded(self):
        old = get_connection(":memory:")
        old.executescript("""
            CREATE TABLE services (id INTEGER PRIMARY KEY, name TEXT UNIQUE, price INTEGER,
                                   duration_minutes INTEGER, refill_days INTEGER);
            CREATE TABLE appointments (id INTEGER PRIMARY KEY, customer_name TEXT, customer_chat INTEGER,
                                       service_id INTEGER, start_time TEXT, end_time TEXT,
                                       status TEXT DEFAULT 'confirmed');
            INSERT INTO services (name, price, duration_minutes) VALUES ('השלמת ציפורן', 10, 15);
        """)
        setup_schema(old)
        addon = old.execute("SELECT * FROM services WHERE name = 'השלמת ציפורן'").fetchone()
        self.assertEqual(addon["is_addon"], 1)
        cols = {r["name"] for r in old.execute("PRAGMA table_info(appointments)")}
        self.assertIn("customer_phone", cols)


if __name__ == "__main__":
    unittest.main()
