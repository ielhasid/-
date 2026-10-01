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

    def slots(self, service, day, now=NOW):
        return [s.strftime("%H:%M") for s in get_available_slots(service, day, now=now, conn=self.conn)]

    def test_empty_day_first_and_last_slot(self):
        s = self.slots(GEL_60, SUNDAY)
        self.assertEqual(s[0], "09:00")
        self.assertEqual(s[-1], "18:00")   # 60 דק' חייבות להסתיים עד 19:00

    def test_closed_on_saturday(self):
        self.assertEqual(self.slots(GEL_60, SATURDAY), [])

    def test_friday_short_day(self):
        self.assertEqual(self.slots(BUILD_90, FRIDAY)[-1], "12:30")   # 90 דק' עד 14:00

    def test_booking_blocks_overlapping_slots(self):
        book_appointment("נועה", BUILD_90, datetime(2026, 10, 4, 10, 0), now=NOW, conn=self.conn)  # 10:00-11:30
        s = self.slots(GEL_60, SUNDAY)
        self.assertIn("09:00", s)       # 09:00-10:00 נגמר בדיוק כשהבנייה מתחילה - מותר
        self.assertNotIn("09:15", s)    # 09:15-10:15 חופף
        self.assertNotIn("11:15", s)
        self.assertIn("11:30", s)       # מתחיל בדיוק כשהבנייה נגמרת - מותר

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

    def test_addon_makes_appointment_longer(self):
        # 60 דק' + 10 דק' תוספת = 70: התור האחרון שנגמר עד 19:00 מתחיל ב-17:45
        s = [t.strftime("%H:%M") for t in get_available_slots(GEL_60, SUNDAY, now=NOW, conn=self.conn, with_addon=True)]
        self.assertEqual(s[-1], "17:45")

    def test_addon_cannot_be_booked_alone(self):
        with self.assertRaises(BookingError):
            book_appointment("נועה", ADDON, datetime(2026, 10, 4, 10, 0), now=NOW, conn=self.conn)

    def test_phone_and_addon_are_saved(self):
        appt = book_appointment("נועה", GEL_60, datetime(2026, 10, 4, 10, 0), now=NOW, conn=self.conn,
                                customer_phone="0501234567", with_addon=True)
        row = self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appt,)).fetchone()
        self.assertEqual(row["customer_phone"], "0501234567")
        self.assertEqual(row["end_time"], "2026-10-04 11:10")

    def test_changed_durations_update_existing_database(self):
        self.conn.execute("UPDATE services SET duration_minutes = 999, price = 1 WHERE id = 4")
        setup_schema(self.conn)
        row = self.conn.execute("SELECT duration_minutes, price FROM services WHERE id = 4").fetchone()
        self.assertEqual((row["duration_minutes"], row["price"]), (90, 250))

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
