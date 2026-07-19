import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import config

BASE = config.BASE_DIR
DB_PATH = config.DB_PATH
IS_POSTGRES = bool(config.DATABASE_URL)
SCHEMA_VERSION = 2
SQLITE_ADDITIONS = {
    "bookings": (
        "google_calendar_event_id TEXT",
        "calendar_sync_status TEXT NOT NULL DEFAULT 'not_connected'",
        "calendar_sync_error TEXT",
        "calendar_synced_at INTEGER",
    ),
    "otp_codes": ("purpose TEXT NOT NULL DEFAULT 'customer'",),
    "sessions": ("csrf_token TEXT",),
    "oauth_states": ("session_hash TEXT", "redirect_uri TEXT"),
}


def now() -> int:
    return int(time.time())


class Connection:
    def __init__(self, raw, *, postgres: bool):
        self.raw = raw
        self.postgres = postgres

    def execute(self, sql: str, params=()):
        if self.postgres:
            sql = sql.replace("BEGIN IMMEDIATE", "BEGIN").replace("?", "%s")
        return self.raw.execute(sql, params)

    def executescript(self, script: str):
        if self.postgres:
            return self.raw.execute(script)
        return self.raw.executescript(script)

    def close(self) -> None:
        self.raw.close()


def connect() -> Connection:
    if IS_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row

        pg_raw = psycopg.connect(
            config.DATABASE_URL,
            autocommit=True,
            connect_timeout=10,
            row_factory=dict_row,
        )
        return Connection(pg_raw, postgres=True)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    sqlite_raw = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    sqlite_raw.row_factory = sqlite3.Row
    sqlite_raw.execute("PRAGMA foreign_keys=ON")
    sqlite_raw.execute("PRAGMA busy_timeout=20000")
    sqlite_raw.execute("PRAGMA journal_mode=WAL")
    sqlite_raw.execute("PRAGMA synchronous=NORMAL")
    return Connection(sqlite_raw, postgres=False)


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: Connection, *, immediate: bool = False):
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def rowdict(row):
    return dict(row) if row else None


def table_columns(conn: Connection, table: str) -> set[str]:
    if conn.postgres:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=?",
            (table,),
        )
        return {row["column_name"] for row in rows}
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_names(conn: Connection) -> set[str]:
    if conn.postgres:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")
        return {row["tablename"] for row in rows}
    return {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _guard_legacy_phone_schema(conn: Connection) -> None:
    tables = _table_names(conn)
    if "customers" in tables and "email" not in table_columns(conn, "customers"):
        raise RuntimeError(
            "Legacy phone-only database detected; export and migrate it explicitly before startup"
        )


def _add_sqlite_column(conn: Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migrate_sqlite(conn: Connection) -> None:
    tables = _table_names(conn)
    for table, definitions in SQLITE_ADDITIONS.items():
        if table not in tables:
            continue
        for definition in definitions:
            _add_sqlite_column(conn, table, definition)


def _sqlite_migration_required(conn: Connection) -> bool:
    tables = _table_names(conn)
    for table, definitions in SQLITE_ADDITIONS.items():
        if table not in tables:
            continue
        columns = table_columns(conn, table)
        if any(definition.split()[0] not in columns for definition in definitions):
            return True
    return False


def _backup_sqlite(conn: Connection) -> None:
    if not DB_PATH.is_file():
        return
    backup_path = DB_PATH.with_name(
        f"{DB_PATH.stem}.pre-migration-{time.time_ns()}{DB_PATH.suffix}"
    )
    with sqlite3.connect(backup_path) as backup:
        conn.raw.backup(backup)


def _schema_path() -> Path:
    return BASE / ("schema_postgres.sql" if IS_POSTGRES else "schema.sql")


def _schema_version(conn: Connection) -> int:
    if "schema_meta" not in _table_names(conn):
        return 0
    row = conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()
    return int(row["version"]) if row else 0


def _set_schema_version(conn: Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "id INTEGER PRIMARY KEY CHECK (id=1),version INTEGER NOT NULL,updated_at BIGINT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (id,version,updated_at) VALUES (1,?,?) "
        "ON CONFLICT (id) DO UPDATE SET version=excluded.version,updated_at=excluded.updated_at",
        (SCHEMA_VERSION, now()),
    )


def init_db() -> None:
    with get_conn() as conn:
        _guard_legacy_phone_schema(conn)
        current_version = _schema_version(conn)
        if current_version > SCHEMA_VERSION:
            raise RuntimeError("Database schema is newer than this application version")
        if current_version == SCHEMA_VERSION:
            return
        if conn.postgres:
            conn.execute("SELECT pg_advisory_lock(71402531)")
        try:
            current_version = _schema_version(conn)
            if current_version > SCHEMA_VERSION:
                raise RuntimeError("Database schema is newer than this application version")
            if current_version == SCHEMA_VERSION:
                return
            if not conn.postgres and "bookings" in _table_names(conn):
                if _sqlite_migration_required(conn):
                    _backup_sqlite(conn)
                _migrate_sqlite(conn)
            conn.executescript(_schema_path().read_text(encoding="utf-8"))
            _seed_defaults(conn)
            _encrypt_legacy_google_token(conn)
            _set_schema_version(conn)
        finally:
            if conn.postgres:
                conn.execute("SELECT pg_advisory_unlock(71402531)")


def _seed_defaults(conn: Connection) -> None:
    conn.execute(
        "INSERT INTO settings (id,name,description,min_lead_minutes,max_days_ahead) "
        "VALUES (1,?,?,120,60) ON CONFLICT (id) DO NOTHING",
        ("יעל - מערכת תורים", "בחרי שירות וקבעי תור בקלות"),
    )
    count = conn.execute("SELECT COUNT(*) AS count FROM working_hours").fetchone()["count"]
    if count == 0:
        for day in range(7):
            closed = 1 if day == 6 else 0
            conn.execute(
                "INSERT INTO working_hours "
                "(day_of_week,is_closed,open_time,close_time,slot_interval_minutes) "
                "VALUES (?,?,?,?,?) ON CONFLICT (day_of_week) DO NOTHING",
                (day, closed, None if closed else "09:00", None if closed else "18:00", 15),
            )
    conn.execute(
        "INSERT INTO services "
        "(id,name,category,price,duration_minutes,display_order) "
        "VALUES (1,?,?,0,30,1) ON CONFLICT (id) DO NOTHING",
        ("פגישת ייעוץ", "כללי"),
    )
    default_services = [
        (1, "בניה בטיפים הפוך", "ציפורניים", 230, 150, 1),
        (2, "לק גל + מבנה אנטומי", "ציפורניים", 110, 90, 2),
        (3, "הסרה לק גל", "ציפורניים", 20, 20, 3),
        (4, "ציור", "תוספות", 10, 10, 4),
        (5, "פרנץ", "תוספות", 10, 20, 5),
        (6, "השלמה", "תוספות", 10, 10, 6),
    ]
    for service_id, name, category, price, duration_minutes, display_order in default_services:
        conn.execute(
            "UPDATE services SET "
            "name=?,category=?,price=?,duration_minutes=?,is_active=1,display_order=? "
            "WHERE id=?",
            (name, category, price, duration_minutes, display_order, service_id),
        )
        conn.execute(
            "INSERT INTO services "
            "(id,name,category,price,duration_minutes,is_active,display_order) "
            "VALUES (?,?,?,?,?,1,?) ON CONFLICT (id) DO NOTHING",
            (service_id, name, category, price, duration_minutes, display_order),
        )
    if conn.postgres:
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('services','id'), "
            "GREATEST((SELECT COALESCE(MAX(id),1) FROM services),1), true)"
        )


def _encrypt_legacy_google_token(conn: Connection) -> None:
    stored = get_secret(conn, "google_refresh_token")
    if not stored:
        return
    import secret_crypto

    if not secret_crypto.is_encrypted(stored):
        set_secret(conn, "google_refresh_token", secret_crypto.encrypt(stored))


def settings(conn: Connection):
    return rowdict(conn.execute("SELECT * FROM settings WHERE id=1").fetchone())


def public_services(conn: Connection):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM services WHERE is_active=1 ORDER BY display_order,id"
        )
    ]


def all_services(conn: Connection):
    return [dict(row) for row in conn.execute("SELECT * FROM services ORDER BY display_order,id")]


def active_services_by_ids(conn: Connection, ids: list[int]):
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM services WHERE is_active=1 AND id IN ({placeholders})",
            ids,
        )
    ]
    order = {service_id: index for index, service_id in enumerate(ids)}
    return sorted(rows, key=lambda row: order.get(row["id"], len(ids)))


def customer_by_email(conn: Connection, email: str):
    return rowdict(conn.execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone())


def customer_by_id(conn: Connection, customer_id: int):
    return rowdict(conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone())


def insert_id(conn: Connection, sql: str, params) -> int:
    if conn.postgres:
        row = conn.execute(f"{sql} RETURNING id", params).fetchone()
        return int(row["id"])
    return int(conn.execute(sql, params).lastrowid)


def upsert_customer(conn: Connection, email: str, name: str) -> int:
    customer = customer_by_email(conn, email)
    if customer:
        return int(customer["id"])
    if conn.postgres:
        row = conn.execute(
            "INSERT INTO customers (email,name,created_at) VALUES (?,?,?) "
            "ON CONFLICT (email) DO UPDATE SET email=excluded.email RETURNING id",
            (email, name.strip(), now()),
        ).fetchone()
        return int(row["id"])
    try:
        return insert_id(
            conn,
            "INSERT INTO customers (email,name,created_at) VALUES (?,?,?)",
            (email, name.strip(), now()),
        )
    except Exception:
        customer = customer_by_email(conn, email)
        if customer:
            return int(customer["id"])
        raise


def booking_overlap(
    conn: Connection,
    starts: int,
    ends: int,
    exclude_booking_id: int | None = None,
) -> bool:
    sql = (
        "SELECT 1 FROM bookings WHERE status IN ('pending','approved') "
        "AND starts_at < ? AND ends_at > ?"
    )
    params: list[int] = [ends, starts]
    if exclude_booking_id is not None:
        sql += " AND id<>?"
        params.append(exclude_booking_id)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def block_overlap(conn: Connection, starts: int, ends: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM blocked_slots WHERE starts_at < ? AND ends_at > ? LIMIT 1",
            (ends, starts),
        ).fetchone()
        is not None
    )


def working_hours(conn: Connection):
    return [dict(row) for row in conn.execute("SELECT * FROM working_hours ORDER BY day_of_week")]


def overrides_between(conn: Connection, start: str, end: str):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM date_overrides WHERE override_date BETWEEN ? AND ? "
            "ORDER BY override_date",
            (start, end),
        )
    ]


def blocks_between(conn: Connection, start: str, end: str):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM blocked_slots WHERE blocked_date BETWEEN ? AND ? "
            "ORDER BY blocked_date,blocked_time",
            (start, end),
        )
    ]


def customer_bookings(conn: Connection, customer_id: int):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM bookings WHERE customer_id=? AND hidden_by_customer=0 "
            "ORDER BY starts_at DESC",
            (customer_id,),
        )
    ]


def owner_bookings(conn: Connection, start: str, end: str, status: str | None = None):
    sql = (
        "SELECT b.*,c.name customer_name,c.email customer_email FROM bookings b "
        "JOIN customers c ON c.id=b.customer_id WHERE b.booking_date BETWEEN ? AND ?"
    )
    params: list[object] = [start, end]
    if status:
        sql += " AND b.status=?"
        params.append(status)
    return [dict(row) for row in conn.execute(sql + " ORDER BY b.starts_at", params)]


def booking_for_customer(conn: Connection, booking_id: int, customer_id: int | None):
    if customer_id is None:
        return None
    return rowdict(
        conn.execute(
            "SELECT * FROM bookings WHERE id=? AND customer_id=?",
            (booking_id, customer_id),
        ).fetchone()
    )


def booking_with_customer(conn: Connection, booking_id: int, *, for_update: bool = False):
    sql = (
        "SELECT b.*,c.name customer_name,c.email customer_email FROM bookings b "
        "JOIN customers c ON c.id=b.customer_id WHERE b.id=?"
    )
    if for_update and conn.postgres:
        sql += " FOR UPDATE OF b"
    return rowdict(conn.execute(sql, (booking_id,)).fetchone())


def insert_booking(
    conn: Connection,
    customer_id: int,
    service_ids: list[int],
    services: list[dict],
    booking_date: str,
    booking_time: str,
    notes: str | None,
    starts: int,
    ends: int,
) -> int:
    snapshot = [
        {
            "name": service["name"],
            "price": service["price"],
            "duration": service["duration_minutes"],
        }
        for service in services
    ]
    timestamp = now()
    return insert_id(
        conn,
        """INSERT INTO bookings
        (customer_id,service_ids,services_snapshot,booking_date,booking_time,
         duration_minutes,price,status,notes,starts_at,ends_at,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            customer_id,
            json.dumps(service_ids),
            json.dumps(snapshot, ensure_ascii=False),
            booking_date,
            booking_time,
            sum(service["duration_minutes"] for service in services),
            sum(service["price"] for service in services),
            "pending",
            notes,
            starts,
            ends,
            timestamp,
            timestamp,
        ),
    )


def log_email(conn: Connection, recipient: str, template: str, status: str) -> None:
    conn.execute(
        "INSERT INTO email_log (recipient,template,provider_status,sent_at) VALUES (?,?,?,?)",
        (recipient, template, status, now()),
    )


def get_secret(conn: Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_secrets WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_secret(conn: Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_secrets (key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, value, now()),
    )


def delete_secret(conn: Connection, key: str) -> None:
    conn.execute("DELETE FROM app_secrets WHERE key=?", (key,))


def create_oauth_state(
    conn: Connection,
    provider: str,
    state: str,
    session_hash: str,
    redirect_uri: str,
    ttl_seconds: int = 600,
) -> None:
    timestamp = now()
    conn.execute("DELETE FROM oauth_states WHERE expires_at<?", (timestamp,))
    conn.execute(
        "INSERT INTO oauth_states "
        "(state,provider,session_hash,redirect_uri,expires_at,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (state, provider, session_hash, redirect_uri, timestamp + ttl_seconds, timestamp),
    )


def consume_oauth_state(
    conn: Connection,
    provider: str,
    state: str,
    session_hash: str,
):
    with transaction(conn, immediate=True):
        sql = "SELECT * FROM oauth_states WHERE provider=? AND state=? AND session_hash=?"
        if conn.postgres:
            sql += " FOR UPDATE"
        row = conn.execute(sql, (provider, state, session_hash)).fetchone()
        conn.execute("DELETE FROM oauth_states WHERE provider=? AND state=?", (provider, state))
    if not row or row["expires_at"] < now():
        return None
    return dict(row)


def enforce_rate_limit(
    conn: Connection,
    scope: str,
    identity_hash: str,
    limit: int,
    window_seconds: int,
) -> bool:
    timestamp = now()
    cutoff = timestamp - window_seconds
    if conn.postgres:
        lock_material = f"{scope}\0{identity_hash}".encode()
        lock_key = int.from_bytes(
            hashlib.blake2b(lock_material, digest_size=8).digest(),
            "big",
            signed=True,
        )
        conn.execute("SELECT pg_advisory_xact_lock(?)", (lock_key,))
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM rate_limit_events "
        "WHERE scope=? AND identity_hash=? AND created_at>?",
        (scope, identity_hash, cutoff),
    ).fetchone()["count"]
    if count >= limit:
        return False
    conn.execute(
        "INSERT INTO rate_limit_events (scope,identity_hash,created_at) VALUES (?,?,?)",
        (scope, identity_hash, timestamp),
    )
    if timestamp % 97 == 0:
        conn.execute("DELETE FROM rate_limit_events WHERE created_at<?", (timestamp - 86400,))
    return True
