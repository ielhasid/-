"""
בדיקות לשרת האתר. הרצה:  python -m unittest test_web.py -v

כל בדיקה רצה על קובץ מסד נתונים זמני, כדי לא לגעת בתורים האמיתיים.
"""

import os
import tempfile
import unittest

import db
import web


class WebTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_file = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.old_path, db.DB_PATH = db.DB_PATH, self.db_file
        db.init_db()
        web.notify_owner = lambda text: None          # לא שולחים הודעות טלגרם אמיתיות בבדיקות
        self.client = web.app.test_client()

    def tearDown(self):
        db.DB_PATH = self.old_path
        os.remove(self.db_file)

    def first_free_slot(self, service_id=2):
        days = self.client.get(f"/api/days?service_id={service_id}").get_json()["days"]
        day = next(d["date"] for d in days if d["available"])
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

    def test_addon_adds_price(self):
        res = self.book(addon=True)
        self.assertEqual(res.get_json()["total"], 130)   # 120 + 10

    def test_garbage_input_returns_400_not_crash(self):
        self.assertEqual(self.client.post("/api/book", data="not json").status_code, 400)
        self.assertEqual(self.client.get("/api/slots?service_id=2&date=banana").status_code, 400)
        self.assertEqual(self.client.get("/api/days").status_code, 400)


if __name__ == "__main__":
    unittest.main()
