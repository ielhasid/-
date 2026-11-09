"""
שכבת מסד הנתונים: יצירת הטבלאות ומילוי נתוני התחלה.

הטבלאות:
- services:       השירותים שהעסק מציע (שם, מחיר, משך)
- working_hours:  השעות הקבועות לכל יום בשבוע (יובל עורכת אותן בעמוד הניהול)
- day_hours:      שעות מיוחדות לתאריך מסוים, שגוברות על השעות הקבועות
- appointments:   התורים שנקבעו
- settings:       הגדרות פנימיות קטנות
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "nails.db"


@contextmanager
def connection(db_path=None):
    """
    חיבור שנסגר תמיד בסוף הבלוק:  with connection() as conn: ...
    חשוב: "with sqlite3.connect()" לבד שומר שינויים אבל *לא סוגר* את החיבור,
    ובווינדוס קובץ עם חיבור פתוח נשאר נעול.
    """
    conn = get_connection(db_path)
    try:
        with conn:          # commit בהצלחה, rollback בשגיאה
            yield conn
    finally:
        conn.close()


def get_connection(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)   # DB_PATH נקרא בזמן הקריאה, כדי שבדיקות יוכלו להחליף אותו
    conn.row_factory = sqlite3.Row  # מאפשר גישה לעמודות לפי שם: row["name"]
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL UNIQUE,
    price            INTEGER NOT NULL,          -- בשקלים
    duration_minutes INTEGER NOT NULL,
    refill_days      INTEGER,                   -- אחרי כמה ימים לשלוח תזכורת מילוי (NULL = לא רלוונטי)
    is_addon         INTEGER NOT NULL DEFAULT 0, -- 1 = תוספת לטיפול אחר (השלמת ציפורן), לא טיפול בפני עצמו
    allows_addon     INTEGER NOT NULL DEFAULT 1  -- 0 = אי אפשר להוסיף השלמת ציפורן (למשל רק רגליים)
);

CREATE TABLE IF NOT EXISTS working_hours (
    weekday    INTEGER PRIMARY KEY,             -- 0=שני ... 6=ראשון (כמו datetime.weekday() בפייתון)
    open_time  TEXT NOT NULL,                   -- "09:00"
    close_time TEXT NOT NULL                    -- "19:00"
);

-- שעות לתאריך מסוים. אם יש שורות לתאריך - הן קובעות, ולא השעות הקבועות.
-- אפשר כמה חלונות ביום (למשל 09:00-13:00 ו-16:00-20:00).
-- שורה אחת עם open_time ריק = יובל סגרה את היום הזה.
CREATE TABLE IF NOT EXISTS day_hours (
    date       TEXT NOT NULL,                   -- "2026-10-08"
    open_time  TEXT,
    close_time TEXT,
    note       TEXT                             -- למשל "חופש" - רק בשביל יובל
);

-- הפסקות קבועות בשבוע, למשל כל יום ראשון 13:00-14:00. אפשר כמה ביום.
CREATE TABLE IF NOT EXISTS weekly_breaks (
    weekday    INTEGER NOT NULL,
    start_time TEXT    NOT NULL,
    end_time   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS day_hours_date ON day_hours (date);

-- "חסימת זמן": זמן תפוס בלי לקוחה מהאתר (תור שנקבע בוואטסאפ, סידור אישי וכו')
CREATE TABLE IF NOT EXISTS blocks (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS blocks_date ON blocks (date);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS appointments (
    id            INTEGER PRIMARY KEY,
    customer_name TEXT    NOT NULL,
    customer_phone TEXT,
    customer_chat INTEGER,                      -- מזהה הצ'אט בטלגרם (כשהתור נקבע דרך הבוט)
    service_id    INTEGER NOT NULL REFERENCES services(id),
    with_addon    INTEGER NOT NULL DEFAULT 0,   -- כמה השלמות ציפורן (0 = בלי)
    reminder_sent_at TEXT,                      -- מתי יובל שלחה תזכורת (NULL = עוד לא)
    deposit_status TEXT,                        -- מקדמה: pending / paid / refunded / kept (NULL = בלי מקדמה)
    deposit_due_at TEXT,                        -- עד מתי צריך להעביר את המקדמה
    cancelled_at   TEXT,
    start_time    TEXT    NOT NULL,             -- "2026-10-04 10:00"
    end_time      TEXT    NOT NULL,
    public_token  TEXT,                         -- קישור אישי ללקוחה לבדוק אם התור אושר
    status        TEXT    NOT NULL DEFAULT 'confirmed'   -- pending (מחכה לאישור של יובל) / confirmed / cancelled
);
"""

# המחירון של יובל. המשכים הם הערכה שלי - כדאי לאמת מול בעלת העסק.
SERVICES = [
    # (שם, מחיר, דקות, ימים לתזכורת מילוי, תוספת?, אפשר להוסיף השלמת ציפורן?)   - משכים מאושרים ע"י יובל
    ("לק ג'ל עם מבנה אנטומי", 140, 60, 21, 0, 1),
    ("לק ג'ל ללא מבנה אנטומי", 120, 60, 21, 0, 1),
    ("מילוי עם ג'ל בנייה",     150, 60, 21, 0, 1),
    ("בנייה מלאה",             250, 90, 21, 0, 1),
    ("השלמת ציפורן",            10,  0, None, 1, 0),   # 0 דקות: נכלל בזמן הטיפול, משפיע רק על המחיר
    ("לק ג'ל ברגליים",         100, 30, 21, 0, 0),   # רק רגליים - בלי השלמת ציפורן
    ("לק ג'ל בידיים + רגליים", 240, 90, 21, 0, 1),
    # שירות חדש מוסיפים בסוף הרשימה, כדי לא לשנות את המספרים (id) של שירותים קיימים
]

# שעות ההתחלה, רק בהפעלה הראשונה. אחר כך יובל משנה אותן בעמוד הניהול.
# ראשון-חמישי 09:00-19:00, שישי 09:00-14:00, שבת סגור (פשוט לא מופיע)
WORKING_HOURS = [
    (6, "09:00", "19:00"),  # ראשון
    (0, "09:00", "19:00"),  # שני
    (1, "09:00", "19:00"),  # שלישי
    (2, "09:00", "19:00"),  # רביעי
    (3, "09:00", "19:00"),  # חמישי
    (4, "09:00", "14:00"),  # שישי
]


def _add_missing_columns(conn):
    """
    "מיגרציה" קטנה: אם יש לך כבר nails.db מגרסה קודמת, מוסיפים לו את העמודות החדשות
    במקום למחוק אותו. בפרויקטים גדולים יש לזה כלים ייעודיים (כמו Alembic).
    """
    wanted = {
        "services": [("is_addon", "INTEGER NOT NULL DEFAULT 0"), ("allows_addon", "INTEGER NOT NULL DEFAULT 1")],
        "appointments": [("customer_phone", "TEXT"), ("with_addon", "INTEGER NOT NULL DEFAULT 0"),
                         ("reminder_sent_at", "TEXT"), ("deposit_status", "TEXT"),
                         ("deposit_due_at", "TEXT"), ("cancelled_at", "TEXT"),
                         ("public_token", "TEXT")],
        "day_hours": [("note", "TEXT")],
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def setup_schema(conn):
    """יוצר טבלאות, מעדכן מסד ישן וממלא נתוני התחלה. בטוח להריץ שוב ושוב."""
    with conn:
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
        conn.executemany(
            # "UPSERT": שירות חדש נוסף, שירות קיים מתעדכן (מחיר, משך).
            # ככה הרשימה בקוד היא מקור האמת, וכדי לשנות מחיר או משך משנים רק אותה.
            "INSERT INTO services (name, price, duration_minutes, refill_days, is_addon, allows_addon) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET price = excluded.price, "
            "duration_minutes = excluded.duration_minutes, refill_days = excluded.refill_days, "
            "is_addon = excluded.is_addon, allows_addon = excluded.allows_addon",
            SERVICES,
        )
        # את השעות הקבועות ממלאים רק פעם אחת. אחרת, אם יובל סוגרת יום בשבוע,
        # ההפעלה הבאה של השרת הייתה "מחזירה" אותו.
        if conn.execute("SELECT 1 FROM settings WHERE key = 'hours_seeded'").fetchone() is None:
            conn.executemany(
                "INSERT OR IGNORE INTO working_hours (weekday, open_time, close_time) VALUES (?, ?, ?)",
                WORKING_HOURS,
            )
            conn.execute("INSERT INTO settings (key, value) VALUES ('hours_seeded', '1')")
        # יום שיובל לא קבעה לו שעות: closed = סגור ללקוחות (ברירת המחדל), weekly = לפי השעות הקבועות
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('unset_days', 'closed')")


def init_db(db_path=None):
    conn = get_connection(db_path)
    setup_schema(conn)
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DB_PATH}")
