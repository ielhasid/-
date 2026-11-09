import urllib.parse
"""
בדיקות לשרת האתר. הרצה:  python -m unittest test_web.py -v

כל בדיקה רצה על קובץ מסד נתונים זמני, כדי לא לגעת בתורים האמיתיים.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

import db
import web
from scheduling import now_local


def next_sunday(min_days_ahead=7):
    """יום ראשון שבטוח פתוח ובטווח: לפחות שבוע קדימה."""
    d = now_local().date() + timedelta(days=min_days_ahead)
    return d + timedelta(days=(6 - d.weekday()) % 7)


class WebTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_file = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.old_path, db.DB_PATH = db.DB_PATH, self.db_file
        db.init_db()
        with db.connection() as c:                    # הבדיקות כאן מניחות שעות קבועות
            c.execute("UPDATE settings SET value = 'weekly' WHERE key = 'unset_days'")
        web.notify_owner = lambda text: None          # לא שולחים הודעות טלגרם אמיתיות בבדיקות
        self.client = web.app.test_client()

    def tearDown(self):
        db.DB_PATH = self.old_path
        os.remove(self.db_file)

    def first_free_slot(self, service_id=2):
        day = next_sunday().isoformat()
        slots = self.client.get(f"/api/slots?service_id={service_id}&date={day}").get_json()["slots"]
        return day, slots[0]

    def book(self, **overrides):
        day, time = self.first_free_slot()
        body = {"service_id": 2, "start": f"{day}T{time}", "name": "נועה", "phone": "050-123 4567"}
        body.update(overrides)
        return self.client.post("/api/book", json=body)

    def test_info_hides_addon_from_services(self):
        info = self.client.get("/api/info").get_json()
        names = [s["name"] for s in info["services"]]
        self.assertNotIn("השלמת ציפורן", names)
        self.assertEqual(info["addon"]["name"], "השלמת ציפורן")

    def test_page_is_served(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("קביעת תור", res.get_data(as_text=True))

    def test_booking_works_and_slot_disappears(self):
        day, time = self.first_free_slot()
        res = self.book()
        self.assertEqual(res.status_code, 201)
        slots = self.client.get(f"/api/slots?service_id=2&date={day}").get_json()["slots"]
        self.assertNotIn(time, slots)

    def test_phone_is_normalized(self):
        self.book(phone="+972 50-123-4567")
        with db.connection() as conn:
            phone = conn.execute("SELECT customer_phone FROM appointments").fetchone()[0]
        self.assertEqual(phone, "0501234567")

    def test_bad_phone_rejected(self):
        res = self.book(phone="12345")
        self.assertEqual(res.status_code, 400)
        self.assertIn("טלפון", res.get_json()["error"])

    def test_missing_name_rejected(self):
        self.assertEqual(self.book(name=" ").status_code, 400)

    def test_double_booking_rejected(self):
        day, time = self.first_free_slot()
        body = {"service_id": 2, "start": f"{day}T{time}", "name": "נועה", "phone": "0501234567"}
        self.assertEqual(self.client.post("/api/book", json=body).status_code, 201)
        res = self.client.post("/api/book", json={**body, "name": "מאיה"})
        self.assertEqual(res.status_code, 400)

    def test_addon_does_not_add_time(self):
        res = self.book(addon=True)                          # גרסה ישנה של העמוד שולחת True
        self.assertEqual(res.get_json()["end"][11:], "10:00")   # השלמה נכללת בזמן הטיפול

    def test_several_addons(self):
        res = self.book(addon=3).get_json()
        self.assertIn("3 השלמות ציפורן", res["service"])
        self.assertEqual(res["end"][11:], "10:00")            # גם 3 השלמות לא מאריכות
        self.assertEqual(self.book(addon=11).status_code, 400)

    def test_slots_with_addon_count(self):
        day = next_sunday().isoformat()
        last = lambda n: self.client.get(f"/api/slots?service_id=2&date={day}&addon={n}").get_json()["slots"][-1]
        self.assertEqual((last(0), last(2)), ("18:00", "18:00"))   # השלמות לא משנות את השעות
        self.assertEqual(self.client.get(f"/api/slots?service_id=2&date={day}&addon=x").status_code, 400)

    def test_garbage_input_returns_400_not_crash(self):
        self.assertEqual(self.client.post("/api/book", data="not json").status_code, 400)
        self.assertEqual(self.client.get("/api/slots?service_id=2&date=banana").status_code, 400)
        self.assertEqual(self.client.get("/api/month").status_code, 400)
        self.assertEqual(self.client.get("/api/month?service_id=2&month=2026-13").status_code, 400)


    # ---------- היומן החודשי ----------

    def month_of(self, d):
        res = self.client.get(f"/api/month?service_id=2&month={d.strftime('%Y-%m')}").get_json()
        return {x["date"]: x["status"] for x in res["days"]}

    def test_month_marks_saturday_closed_and_sunday_available(self):
        sunday = next_sunday()
        saturday = sunday - timedelta(days=1)
        statuses = self.month_of(sunday)
        self.assertEqual(statuses[sunday.isoformat()], "available")
        if saturday.month == sunday.month:
            self.assertEqual(statuses[saturday.isoformat()], "none")

    def test_month_marks_past_days(self):
        today = now_local().date()
        statuses = self.month_of(today)
        if today.day > 1:
            self.assertEqual(statuses[today.replace(day=1).isoformat()], "past")

    def test_month_has_every_day(self):
        statuses = self.month_of(date(2027, 2, 1) if now_local().date() < date(2027, 1, 1) else next_sunday())
        self.assertIn(len(statuses), (28, 29, 30, 31))

    def test_booking_far_future_is_rejected(self):
        far = web.last_bookable_day() + timedelta(days=7)
        res = self.client.post("/api/book", json={"service_id": 2, "start": f"{far}T10:00",
                                                  "name": "נועה", "phone": "0501234567"})
        self.assertEqual(res.status_code, 400)
        statuses = self.month_of(far)
        self.assertEqual(statuses[far.isoformat()], "out_of_range")

    def test_full_day_looks_like_closed(self):
        sunday = next_sunday()
        # ממלאים את כל היום בתורים של שעה
        for hour in range(9, 19):
            self.client.post("/api/book", json={"service_id": 2, "start": f"{sunday}T{hour:02d}:00",
                                                "name": "נועה", "phone": "0501234567"})
        self.assertEqual(self.month_of(sunday)[sunday.isoformat()], "none")      # מלא נראה כמו סגור

    def test_info_includes_refill_days_and_range(self):
        info = self.client.get("/api/info").get_json()
        self.assertTrue(all(s["refill_days"] == 21 for s in info["services"]))
        self.assertLessEqual(info["today"], info["last_day"])


class ApprovalTests(unittest.TestCase):
    """כל תור מהאתר הוא בקשה: השעה נשמרת, יובל מאשרת או דוחה, והלקוחה בודקת בקישור האישי."""

    def setUp(self):
        fd, self.db_file = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.old_path, db.DB_PATH = db.DB_PATH, self.db_file
        db.init_db()
        with db.connection() as c:
            c.execute("UPDATE settings SET value = 'weekly' WHERE key = 'unset_days'")
        self.sent = []
        web.notify_owner = self.sent.append
        self.old_pw = getattr(web.config, "ADMIN_PASSWORD", None)
        web.config.ADMIN_PASSWORD = "pw"
        self.client = web.app.test_client()
        self.day = next_sunday()

    def tearDown(self):
        web.config.ADMIN_PASSWORD = self.old_pw
        db.DB_PATH = self.old_path
        os.remove(self.db_file)

    def book(self, time="10:00"):
        return self.client.post("/api/book", json={"service_id": 2, "start": f"{self.day}T{time}",
                                                   "name": "נועה כהן", "phone": "0501234567"}).get_json()

    def status(self, token):
        return self.client.get(f"/api/booking/{token}").get_json()["status"]

    def test_booking_is_pending_and_holds_the_slot(self):
        res = self.book()
        self.assertEqual(res["status"], "pending")
        self.assertIn("מחכה לאישור", self.sent[0])
        slots = self.client.get(f"/api/slots?service_id=2&date={self.day}").get_json()["slots"]
        self.assertNotIn("10:00", slots)                       # אף אחת אחרת לא יכולה לבקש את אותה שעה

    def test_customer_link_shows_status(self):
        res = self.book()
        self.assertEqual(self.status(res["token"]), "pending")
        self.assertEqual(self.client.get("/api/booking/not-a-real-token").status_code, 404)

    def test_info_has_no_deposit_and_no_prices(self):
        info = self.client.get("/api/info").get_json()
        self.assertNotIn("deposit", info)
        self.assertNotIn("price", info["services"][0])          # הלקוחות לא רואות מחירים
        self.assertEqual(info["addon"]["price"], 10)                # רק התוספת של השלמת ציפורן מוצגת

    def test_confirm(self):
        res = self.book()
        self.client.post("/api/admin/login", json={"password": "pw"})
        pending = self.client.get("/api/admin/pending").get_json()["pending"]
        self.assertEqual([p["id"] for p in pending], [res["id"]])
        out = self.client.post(f"/api/admin/appointments/{res['id']}/confirm", json={}).get_json()
        self.assertTrue(out["whatsapp_url"].startswith("https://wa.me/972501234567?text="))
        self.assertEqual(self.status(res["token"]), "confirmed")
        self.assertEqual(self.client.get("/api/admin/pending").get_json()["pending"], [])
        # אישור פעמיים - שגיאה ברורה ולא קריסה
        self.assertEqual(self.client.post(f"/api/admin/appointments/{res['id']}/confirm", json={}).status_code, 400)

    def test_feet_only_has_no_addon(self):
        services = {x["name"]: x for x in self.client.get("/api/info").get_json()["services"]}
        self.assertEqual(services["לק ג'ל ברגליים"]["allows_addon"], 0)
        self.assertEqual(services["לק ג'ל בידיים + רגליים"]["allows_addon"], 1)
        feet = services["לק ג'ל ברגליים"]["id"]
        res = self.client.post("/api/book", json={"service_id": feet, "start": f"{self.day}T09:00", "addon": 1,
                                                  "name": "דנה", "phone": "0501234567"})
        self.assertEqual(res.status_code, 400)

    def test_approval_message_has_price(self):
        res = self.client.post("/api/book", json={"service_id": 1, "start": f"{self.day}T09:00", "addon": 2,
                                                  "name": "huck", "phone": "0501234567"}).get_json()
        self.client.post("/api/admin/login", json={"password": "pw"})
        url = self.client.post(f"/api/admin/appointments/{res['id']}/confirm", json={}).get_json()["whatsapp_url"]
        text = urllib.parse.unquote(url)
        self.assertIn("עלות התור: 160 ₪ (140 ₪ + 2 השלמות ציפורן × 10 ₪)", text)
        self.assertIn("*תשלום במזומן במעמד הטיפול.*", text)

    def test_decline_frees_slot(self):
        res = self.book()
        self.client.post("/api/admin/login", json={"password": "pw"})
        out = self.client.post(f"/api/admin/appointments/{res['id']}/cancel", json={}).get_json()
        self.assertIn("wa.me", out["whatsapp_url"])
        self.assertEqual(self.status(res["token"]), "cancelled")
        slots = self.client.get(f"/api/slots?service_id=2&date={self.day}").get_json()["slots"]
        self.assertIn("10:00", slots)

    def test_manual_appointment_is_confirmed_right_away(self):
        self.client.post("/api/admin/login", json={"password": "pw"})
        self.client.post("/api/admin/appointments", json={"service_id": 1, "date": self.day.isoformat(),
                                                          "time": "09:00", "name": "רותם"})
        appts = self.client.get(f"/api/admin/appointments?date={self.day}").get_json()["appointments"]
        self.assertFalse(appts[0]["pending"])

    def test_pending_has_no_reminder_link(self):
        self.book()
        self.client.post("/api/admin/login", json={"password": "pw"})
        a = self.client.get(f"/api/admin/appointments?date={self.day}").get_json()["appointments"][0]
        self.assertTrue(a["pending"])
        self.assertNotIn("whatsapp_url", a)


class CalendarTests(ApprovalTests):
    """יומן מנוי ליובל + קובץ יומן ללקוחה."""

    def test_owner_feed_has_only_confirmed(self):
        first, second = self.book("10:00"), self.book("12:00")
        self.client.post("/api/admin/login", json={"password": "pw"})
        self.client.post(f"/api/admin/appointments/{first['id']}/confirm", json={})
        link = self.client.get("/api/admin/calendar").get_json()
        self.assertTrue(link["webcal"].startswith("webcal://"))
        path = link["url"].split("localhost", 1)[1]
        self.client.post("/api/admin/logout", json={})               # האייפון לא מחובר - הקישור הסודי מספיק
        body = self.client.get(path).get_data(as_text=True)
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertEqual(body.count("BEGIN:VEVENT"), 1)               # הבקשה שלא אושרה לא ביומן
        self.assertIn("נועה כהן", body)
        # שעון ישראל -> UTC: 10:00 בחורף = 08:00Z, בקיץ = 07:00Z
        self.assertRegex(body, r"DTSTART:\d{8}T0[78]0000Z")
        self.assertEqual(self.client.get("/calendar/wrong-token.ics").status_code, 404)

    def test_customer_ics_only_after_approval(self):
        res = self.book()
        self.assertEqual(self.client.get(f"/booking/{res['token']}.ics").status_code, 404)
        self.client.post("/api/admin/login", json={"password": "pw"})
        out = self.client.post(f"/api/admin/appointments/{res['id']}/confirm", json={}).get_json()
        self.assertIn("booking%3D" + res["token"], out["whatsapp_url"])   # הקישור ליומן בתוך הודעת האישור
        r = self.client.get(f"/booking/{res['token']}.ics")
        self.assertEqual((r.status_code, r.mimetype), (200, "text/calendar"))
        self.assertIn("TRIGGER:-P1D", r.get_data(as_text=True))       # תזכורת יום לפני


class NotifyTests(unittest.TestCase):
    def test_one_or_many_chat_ids(self):
        old = getattr(web.config, "OWNER_CHAT_ID", None)
        try:
            for value, expected in [(None, []), (111, [111]), ([111, 222], [111, 222]), ([111, None], [111])]:
                web.config.OWNER_CHAT_ID = value
                self.assertEqual(web.owner_chat_ids(), expected)
        finally:
            web.config.OWNER_CHAT_ID = old


class AdminTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_file = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.old_path, db.DB_PATH = db.DB_PATH, self.db_file
        db.init_db()
        with db.connection() as c:                    # הבדיקות כאן מניחות שעות קבועות
            c.execute("UPDATE settings SET value = 'weekly' WHERE key = 'unset_days'")
        web.notify_owner = lambda text: None
        self.old_pw = getattr(web.config, "ADMIN_PASSWORD", None)
        web.config.ADMIN_PASSWORD = "correct-horse"
        self.client = web.app.test_client()
        self.day = next_sunday()
        res = self.client.post("/api/book", json={"service_id": 2, "start": f"{self.day}T10:00",
                                                  "name": "נועה כהן", "phone": "050-1234567"})
        self.appt_id = res.get_json()["id"]

    def tearDown(self):
        web.config.ADMIN_PASSWORD = self.old_pw
        db.DB_PATH = self.old_path
        os.remove(self.db_file)

    def login(self, password="correct-horse"):
        return self.client.post("/api/admin/login", json={"password": password})

    def list_day(self):
        return self.client.get(f"/api/admin/appointments?date={self.day}")

    def test_requires_login(self):
        self.assertEqual(self.list_day().status_code, 401)
        self.assertEqual(self.client.post(f"/api/admin/appointments/{self.appt_id}/cancel", json={}).status_code, 401)

    def test_wrong_password(self):
        self.assertEqual(self.login("guess").status_code, 401)
        self.assertEqual(self.list_day().status_code, 401)

    def test_disabled_without_password(self):
        web.config.ADMIN_PASSWORD = None
        self.assertEqual(self.login().status_code, 503)

    def test_list_shows_appointment_with_whatsapp_reminder(self):
        self.login()
        self.client.post(f"/api/admin/appointments/{self.appt_id}/confirm", json={})
        appts = self.list_day().get_json()["appointments"]
        self.assertEqual(len(appts), 1)
        a = appts[0]
        self.assertEqual((a["name"], a["start"], a["phone"]), ("נועה כהן", "10:00", "0501234567"))
        self.assertTrue(a["whatsapp_url"].startswith("https://wa.me/972501234567?text="))
        self.assertFalse(a["reminder_sent"])

    def test_admin_shows_addon_count_and_price(self):
        self.client.post("/api/book", json={"service_id": 2, "start": f"{self.day}T15:00", "addon": 2,
                                            "name": "מאיה", "phone": "0521111111"})
        self.login()
        a = [x for x in self.list_day().get_json()["appointments"] if x["name"] == "מאיה"][0]
        self.assertEqual((a["price"], a["end"]), (140, "16:00"))     # מחיר עולה, הזמן לא
        self.assertIn("2 השלמות ציפורן", a["service"])

    def test_mark_reminded(self):
        self.login()
        self.client.post(f"/api/admin/appointments/{self.appt_id}/confirm", json={})
        self.assertEqual(self.client.post(f"/api/admin/appointments/{self.appt_id}/reminded", json={}).status_code, 200)
        self.assertTrue(self.list_day().get_json()["appointments"][0]["reminder_sent"])

    def test_cancel_frees_slot(self):
        self.login()
        self.assertEqual(self.client.post(f"/api/admin/appointments/{self.appt_id}/cancel", json={}).status_code, 200)
        self.assertEqual(self.list_day().get_json()["appointments"], [])
        slots = self.client.get(f"/api/slots?service_id=2&date={self.day}").get_json()["slots"]
        self.assertIn("10:00", slots)

    def test_post_without_json_is_rejected(self):
        # הגנה מטפסים של אתרים זרים
        self.login()
        res = self.client.post(f"/api/admin/appointments/{self.appt_id}/cancel", data={"x": "1"})
        self.assertEqual(res.status_code, 400)

    def test_logout(self):
        self.login()
        self.client.post("/api/admin/logout", json={})
        self.assertEqual(self.list_day().status_code, 401)

    def add(self, **overrides):
        body = {"service_id": 1, "date": self.day.isoformat(), "time": "12:00", "name": "רותם", "phone": ""}
        body.update(overrides)
        return self.client.post("/api/admin/appointments", json=body)

    def test_manual_add_requires_login(self):
        self.assertEqual(self.add().status_code, 401)

    def test_manual_add_without_phone(self):
        self.login()
        self.assertEqual(self.add().status_code, 201)
        a = [x for x in self.list_day().get_json()["appointments"] if x["name"] == "רותם"][0]
        self.assertEqual((a["start"], a["phone"]), ("12:00", None))
        self.assertNotIn("whatsapp_url", a)                     # אין טלפון - אין כפתור תזכורת

    def test_manual_add_with_phone_and_addon(self):
        self.login()
        # נועה ב-10:00-11:00, אז התור הבא בשרשרת מתחיל ב-11:00
        self.assertEqual(self.add(phone="052-111-2222", addon=2, time="11:00").status_code, 201)
        a = [x for x in self.list_day().get_json()["appointments"] if x["name"] == "רותם"][0]
        self.assertEqual((a["phone"], a["end"], a["price"]), ("0521112222", "12:00", 160))

    def test_manual_add_rejects_bad_input(self):
        self.login()
        self.assertEqual(self.add(phone="123").status_code, 400)
        self.assertEqual(self.add(name="").status_code, 400)
        self.assertEqual(self.add(time="10:00").status_code, 400)   # השעה תפוסה ע"י נועה
        self.assertEqual(self.add(date="").status_code, 400)

    # ---------- שעות פעילות ----------

    def test_hours_require_login(self):
        self.assertEqual(self.client.get("/api/admin/hours").status_code, 401)
        self.assertEqual(self.client.post("/api/admin/day", json={}).status_code, 401)

    def test_weekly_hours_roundtrip(self):
        self.login()
        weekly = self.client.get("/api/admin/hours").get_json()["weekly"]
        self.assertEqual(weekly[0]["name"], "ראשון")
        self.assertIsNone(weekly[-1]["open"])                       # שבת סגור
        new = [{"weekday": d["weekday"], "open": "10:00", "close": "17:00"} if d["weekday"] == 6
               else {"weekday": d["weekday"], "closed": True} for d in weekly]
        self.assertEqual(self.client.post("/api/admin/hours", json={"weekly": new}).status_code, 200)
        slots = self.client.get(f"/api/slots?service_id=2&date={self.day}").get_json()["slots"]
        self.assertEqual((slots[0], slots[-1]), ("11:00", "16:00"))  # 10:00 תפוס ע"י נועה
        monday = self.day + timedelta(days=1)
        self.assertEqual(self.client.get(f"/api/slots?service_id=2&date={monday}").get_json()["slots"], [])

    def test_bad_hours_rejected(self):
        self.login()
        bad = {"weekly": [{"weekday": 6, "open": "17:00", "close": "10:00"}]}
        self.assertEqual(self.client.post("/api/admin/hours", json=bad).status_code, 400)
        bad = {"date": self.day.isoformat(), "mode": "custom",
               "windows": [{"open": "09:00", "close": "13:00"}, {"open": "12:00", "close": "15:00"}]}
        self.assertEqual(self.client.post("/api/admin/day", json=bad).status_code, 400)

    def test_close_day_warns_about_existing_appointment(self):
        self.login()
        res = self.client.post("/api/admin/day", json={"date": self.day.isoformat(), "mode": "closed"}).get_json()
        self.assertEqual(len(res["outside"]), 1)                    # התור של נועה לא בוטל, רק הזהרה
        self.assertEqual(len(self.list_day().get_json()["appointments"]), 1)
        month = self.client.get(f"/api/month?service_id=2&month={self.day:%Y-%m}").get_json()["days"]
        self.assertEqual({d["date"]: d["status"] for d in month}[self.day.isoformat()], "none")

    def test_vacation_range_and_back_to_regular(self):
        self.login()
        until = self.day + timedelta(days=3)
        self.client.post("/api/admin/day", json={"date": self.day.isoformat(), "until": until.isoformat(), "mode": "closed"})
        for i in range(4):
            d = self.day + timedelta(days=i)
            self.assertEqual(self.client.get(f"/api/slots?service_id=2&date={d}").get_json()["slots"], [], d)
        self.client.post("/api/admin/day", json={"date": self.day.isoformat(), "mode": "regular"})
        day = self.client.get(f"/api/admin/day?date={self.day}").get_json()
        self.assertFalse(day["custom"])
        self.assertEqual(day["windows"], [{"open": "09:00", "close": "19:00"}])

    def test_custom_windows_open_saturday(self):
        self.login()
        saturday = self.day - timedelta(days=1)
        res = self.client.post("/api/admin/day", json={"date": saturday.isoformat(), "mode": "custom",
                                                       "windows": [{"open": "19:00", "close": "22:00"}]})
        self.assertEqual(res.status_code, 200)
        slots = self.client.get(f"/api/slots?service_id=2&date={saturday}").get_json()["slots"]
        self.assertEqual((slots[0], slots[-1]), ("19:00", "21:00"))

    def test_weekly_breaks_roundtrip(self):
        self.login()
        weekly = self.client.get("/api/admin/hours").get_json()["weekly"]
        sunday = next(d for d in weekly if d["weekday"] == 6)
        sunday["breaks"] = [{"start": "13:00", "end": "14:00"}]
        self.assertEqual(self.client.post("/api/admin/hours", json={"weekly": weekly}).status_code, 200)
        again = self.client.get("/api/admin/hours").get_json()["weekly"]
        self.assertEqual(next(d for d in again if d["weekday"] == 6)["breaks"], [{"start": "13:00", "end": "14:00"}])
        slots = self.client.get(f"/api/slots?service_id=2&date={self.day}").get_json()["slots"]
        self.assertNotIn("13:00", slots)
        self.assertIn("14:00", slots)

    def test_break_outside_hours_rejected(self):
        self.login()
        weekly = self.client.get("/api/admin/hours").get_json()["weekly"]
        weekly[0]["breaks"] = [{"start": "20:00", "end": "21:00"}]
        res = self.client.post("/api/admin/hours", json={"weekly": weekly})
        self.assertEqual(res.status_code, 400)
        self.assertIn("בתוך שעות העבודה", res.get_json()["error"])

    def test_booking_until_setting(self):
        self.login()
        info = self.client.get("/api/info").get_json()
        self.assertTrue(info["last_day"].endswith("-12-31"))           # ברירת מחדל: סוף דצמבר
        until = self.day + timedelta(days=2)
        self.assertEqual(self.client.post("/api/admin/booking-until", json={"date": until.isoformat()}).status_code, 200)
        self.assertEqual(self.client.get("/api/info").get_json()["last_day"], until.isoformat())
        far = until + timedelta(days=1)
        self.assertEqual(self.client.get(f"/api/slots?service_id=2&date={far}").status_code, 400)
        self.assertEqual(self.client.post("/api/admin/booking-until", json={"date": "2000-01-01"}).status_code, 400)

    def test_periods_are_grouped(self):
        self.login()
        d = self.day + timedelta(days=7)
        self.client.post("/api/admin/day", json={"date": d.isoformat(), "until": (d + timedelta(days=4)).isoformat(),
                                                 "mode": "closed", "note": "חופש"})
        sat = d + timedelta(days=13)
        self.client.post("/api/admin/day", json={"date": sat.isoformat(), "mode": "custom",
                                                 "windows": [{"open": "19:00", "close": "22:00"}]})
        periods = self.client.get("/api/admin/periods").get_json()["periods"]
        self.assertEqual(len(periods), 2)
        self.assertEqual((periods[0]["from"], periods[0]["until"], periods[0]["note"], periods[0]["closed"]),
                         (d.isoformat(), (d + timedelta(days=4)).isoformat(), "חופש", True))
        self.assertEqual(periods[1]["windows"], [{"open": "19:00", "close": "22:00"}])
        # ביטול התקופה = חזרה לשעות רגילות לכל הטווח
        self.client.post("/api/admin/day", json={"date": d.isoformat(), "until": (d + timedelta(days=4)).isoformat(),
                                                 "mode": "regular"})
        self.assertEqual(len(self.client.get("/api/admin/periods").get_json()["periods"]), 1)

    # ---------- יומן זמינות וחסימות ----------

    def test_availability_month(self):
        self.login()
        res = self.client.get(f"/api/admin/availability?month={self.day:%Y-%m}").get_json()
        day = {d["date"]: d for d in res["days"]}[self.day.isoformat()]
        self.assertEqual((day["appointments"], day["set"]), (1, False))
        self.assertEqual(day["windows"], [{"open": "09:00", "close": "19:00"}])   # מצב weekly בבדיקות
        self.assertEqual(res["mode"], "weekly")

    def test_closed_mode_and_open_until(self):
        self.login()
        self.assertEqual(self.client.post("/api/admin/settings", json={"unset_days": "closed"}).status_code, 200)
        today = now_local().date()
        self.client.post("/api/admin/day", json={"date": today.isoformat(), "until": (today + timedelta(days=5)).isoformat(),
                                                 "mode": "custom", "windows": [{"open": "09:00", "close": "14:00"}]})
        res = self.client.get(f"/api/admin/availability?month={today:%Y-%m}").get_json()
        self.assertEqual(res["open_until"], (today + timedelta(days=5)).isoformat())
        later = today + timedelta(days=8)
        self.assertEqual(self.client.get(f"/api/slots?service_id=2&date={later}").get_json()["slots"], [])

    def test_copy_week(self):
        self.login()
        src = self.day + timedelta(days=7)
        self.client.post("/api/admin/day", json={"date": src.isoformat(), "mode": "custom",
                                                 "windows": [{"open": "14:00", "close": "19:00"}]})
        dst = src + timedelta(days=7)
        self.assertEqual(self.client.post("/api/admin/copy-week", json={"from": src.isoformat(), "to": dst.isoformat()}).status_code, 200)
        day = self.client.get(f"/api/admin/day?date={dst}").get_json()
        self.assertEqual(day["windows"], [{"open": "14:00", "close": "19:00"}])
        self.assertEqual(self.client.post("/api/admin/copy-week", json={"from": src.isoformat(), "to": src.isoformat()}).status_code, 400)

    def test_blocks(self):
        self.login()
        res = self.client.post("/api/admin/blocks", json={"date": self.day.isoformat(), "start": "12:00", "end": "13:30", "note": "דנה"})
        self.assertEqual(res.status_code, 201)
        slots = self.client.get(f"/api/slots?service_id=2&date={self.day}").get_json()["slots"]
        self.assertNotIn("12:00", slots)
        self.assertIn("13:30", slots)
        day = self.list_day().get_json()
        self.assertEqual(day["blocks"][0]["note"], "דנה")
        self.assertIn({"start": "13:30", "end": "19:00"}, day["gaps"])
        # חסימה על תור קיים נדחית
        clash = self.client.post("/api/admin/blocks", json={"date": self.day.isoformat(), "start": "10:30", "end": "11:00"})
        self.assertEqual(clash.status_code, 400)
        # מחיקה מחזירה את הזמן
        self.client.post(f"/api/admin/blocks/{day['blocks'][0]['id']}/delete", json={})
        self.assertIn("12:00", self.client.get(f"/api/slots?service_id=2&date={self.day}").get_json()["slots"])

    def test_admin_page_served(self):
        self.assertEqual(self.client.get("/admin").status_code, 200)


if __name__ == "__main__":
    unittest.main()
