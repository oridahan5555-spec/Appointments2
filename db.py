import json
import sqlite3
import time
from contextlib import contextmanager

import config

BASE = config.BASE_DIR
DB_PATH = config.DB_PATH


def now() -> int:
    return int(time.time())


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def rowdict(row):
    return dict(row) if row else None


def table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_empty_phone_identity(conn):
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "customers" not in tables or "email" in table_columns(conn, "customers"):
        return
    if conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] or conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]:
        raise RuntimeError("Email identity migration requires exporting existing customer data first")
    conn.execute("PRAGMA foreign_keys=OFF")
    for table in ("notification_jobs", "email_log", "sms_log", "sessions", "otp_codes", "otp_requests", "bookings", "customers"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA foreign_keys=ON")


def init_db():
    with get_conn() as conn:
        migrate_empty_phone_identity(conn)
        conn.executescript((BASE / "schema.sql").read_text(encoding="utf-8"))
        if "google_calendar_event_id" not in table_columns(conn, "bookings"):
            conn.execute("ALTER TABLE bookings ADD COLUMN google_calendar_event_id TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO settings (id,name,description,min_lead_minutes,max_days_ahead) VALUES (1,?,?,120,60)",
            ("יעל - מערכת תורים", "בחרי שירות וקבעי תור בקלות"),
        )
        if conn.execute("SELECT COUNT(*) FROM working_hours").fetchone()[0] == 0:
            for day in range(7):
                closed = 1 if day == 6 else 0
                conn.execute("INSERT INTO working_hours VALUES (?,?,?,?,?)", (day, closed, None if closed else "09:00", None if closed else "18:00", 15))
        conn.execute(
            "INSERT OR IGNORE INTO services (id,name,category,price,duration_minutes,display_order) VALUES (1,?,?,0,30,1)",
            ("פגישת ייעוץ", "כללי"),
        )


def settings(conn):
    return rowdict(conn.execute("SELECT * FROM settings WHERE id=1").fetchone())


def public_services(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM services WHERE is_active=1 ORDER BY display_order,id")]


def all_services(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM services ORDER BY display_order,id")]


def active_services_by_ids(conn, ids):
    if not ids:
        return []
    q = ",".join("?" for _ in ids)
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM services WHERE is_active=1 AND id IN ({q})", ids)]
    order = {sid: i for i, sid in enumerate(ids)}
    return sorted(rows, key=lambda row: order.get(row["id"], 999))


def customer_by_email(conn, email):
    return rowdict(conn.execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone())


def customer_by_id(conn, customer_id):
    return rowdict(conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone())


def upsert_customer(conn, email, name):
    customer = customer_by_email(conn, email)
    if customer:
        return customer["id"]
    cursor = conn.execute("INSERT INTO customers (email,name,created_at) VALUES (?,?,?)", (email, name.strip(), now()))
    return cursor.lastrowid


def booking_overlap(conn, starts, ends):
    return conn.execute("SELECT 1 FROM bookings WHERE status IN ('pending','approved') AND starts_at < ? AND ends_at > ? LIMIT 1", (ends, starts)).fetchone() is not None


def block_overlap(conn, starts, ends):
    return conn.execute("SELECT 1 FROM blocked_slots WHERE starts_at < ? AND ends_at > ? LIMIT 1", (ends, starts)).fetchone() is not None


def working_hours(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM working_hours ORDER BY day_of_week")]


def overrides_between(conn, start, end):
    return [dict(row) for row in conn.execute("SELECT * FROM date_overrides WHERE override_date BETWEEN ? AND ? ORDER BY override_date", (start, end))]


def blocks_between(conn, start, end):
    return [dict(row) for row in conn.execute("SELECT * FROM blocked_slots WHERE blocked_date BETWEEN ? AND ? ORDER BY blocked_date,blocked_time", (start, end))]


def customer_bookings(conn, customer_id):
    return [dict(row) for row in conn.execute("SELECT * FROM bookings WHERE customer_id=? AND hidden_by_customer=0 ORDER BY starts_at DESC", (customer_id,))]


def owner_bookings(conn, start, end, status=None):
    sql = "SELECT b.*,c.name customer_name,c.email customer_email FROM bookings b JOIN customers c ON c.id=b.customer_id WHERE b.booking_date BETWEEN ? AND ?"
    args = [start, end]
    if status:
        sql += " AND b.status=?"
        args.append(status)
    sql += " ORDER BY b.starts_at"
    return [dict(row) for row in conn.execute(sql, args)]


def booking_for_customer(conn, booking_id, customer_id):
    return rowdict(conn.execute("SELECT * FROM bookings WHERE id=? AND customer_id=?", (booking_id, customer_id)).fetchone())


def booking_with_customer(conn, booking_id):
    return rowdict(conn.execute("SELECT b.*,c.name customer_name,c.email customer_email FROM bookings b JOIN customers c ON c.id=b.customer_id WHERE b.id=?", (booking_id,)).fetchone())


def insert_booking(conn, customer_id, service_ids, services, booking_date, booking_time, notes, starts, ends):
    snapshot = [{"name": service["name"], "price": service["price"], "duration": service["duration_minutes"]} for service in services]
    timestamp = now()
    cursor = conn.execute(
        """INSERT INTO bookings
        (customer_id,service_ids,services_snapshot,booking_date,booking_time,duration_minutes,price,status,notes,starts_at,ends_at,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (customer_id, json.dumps(service_ids), json.dumps(snapshot, ensure_ascii=False), booking_date, booking_time,
         sum(service["duration_minutes"] for service in services), sum(service["price"] for service in services),
         "pending", notes, starts, ends, timestamp, timestamp),
    )
    return cursor.lastrowid


def log_email(conn, recipient, template, status):
    conn.execute("INSERT INTO email_log (recipient,template,provider_status,sent_at) VALUES (?,?,?,?)", (recipient, template, status, now()))


def get_secret(conn, key):
    row = conn.execute("SELECT value FROM app_secrets WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_secret(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO app_secrets (key,value,updated_at) VALUES (?,?,?)",
        (key, value, now()),
    )


def delete_secret(conn, key):
    conn.execute("DELETE FROM app_secrets WHERE key=?", (key,))


def create_oauth_state(conn, provider, state, ttl_seconds=600):
    timestamp = now()
    conn.execute("DELETE FROM oauth_states WHERE expires_at<?", (timestamp,))
    conn.execute(
        "INSERT INTO oauth_states (state,provider,expires_at,created_at) VALUES (?,?,?,?)",
        (state, provider, timestamp + ttl_seconds, timestamp),
    )


def consume_oauth_state(conn, provider, state):
    row = conn.execute(
        "SELECT * FROM oauth_states WHERE provider=? AND state=?",
        (provider, state),
    ).fetchone()
    conn.execute("DELETE FROM oauth_states WHERE provider=? AND state=?", (provider, state))
    return bool(row and row["expires_at"] >= now())
