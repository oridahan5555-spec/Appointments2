PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT NOT NULL,
  description TEXT,
  address TEXT,
  phone TEXT,
  social_url TEXT,
  waze_url TEXT,
  cover_image TEXT,
  profile_image TEXT,
  preparation_message TEXT,
  min_lead_minutes INTEGER NOT NULL DEFAULT 120,
  max_days_ahead INTEGER NOT NULL DEFAULT 60
);

CREATE TABLE IF NOT EXISTS services (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,
  price INTEGER NOT NULL DEFAULT 0,
  duration_minutes INTEGER NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS working_hours (
  day_of_week INTEGER PRIMARY KEY CHECK (day_of_week BETWEEN 0 AND 6),
  is_closed INTEGER NOT NULL DEFAULT 0,
  open_time TEXT,
  close_time TEXT,
  slot_interval_minutes INTEGER NOT NULL DEFAULT 15
);

CREATE TABLE IF NOT EXISTS date_overrides (
  override_date TEXT PRIMARY KEY,
  is_closed INTEGER NOT NULL DEFAULT 0,
  open_time TEXT,
  close_time TEXT,
  slot_interval_minutes INTEGER,
  internal_note TEXT
);

CREATE TABLE IF NOT EXISTS blocked_slots (
  id INTEGER PRIMARY KEY,
  blocked_date TEXT NOT NULL,
  blocked_time TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL DEFAULT 60,
  internal_note TEXT,
  starts_at INTEGER NOT NULL,
  ends_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocked_range ON blocked_slots (starts_at, ends_at);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  internal_note TEXT,
  is_blocked INTEGER NOT NULL DEFAULT 0,
  no_show_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  service_ids TEXT NOT NULL,
  services_snapshot TEXT NOT NULL,
  booking_date TEXT NOT NULL,
  booking_time TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL,
  price INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled')),
  arrival_status TEXT CHECK (arrival_status IN ('requested','confirmed','declined','no_show')),
  notes TEXT,
  starts_at INTEGER NOT NULL,
  ends_at INTEGER NOT NULL,
  google_calendar_event_id TEXT,
  hidden_by_customer INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_range ON bookings (starts_at, ends_at, status);
CREATE INDEX IF NOT EXISTS idx_bookings_cust ON bookings (customer_id);

CREATE TABLE IF NOT EXISTS otp_codes (
  email TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS otp_requests (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  ip TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_otp_req_email ON otp_requests (email, created_at);
CREATE INDEX IF NOT EXISTS idx_otp_req_ip ON otp_requests (ip, created_at);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  email TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('customer','owner')),
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS email_log (
  id INTEGER PRIMARY KEY,
  recipient TEXT NOT NULL,
  template TEXT NOT NULL,
  provider_status TEXT NOT NULL,
  sent_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS app_secrets (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_jobs (
  id INTEGER PRIMARY KEY,
  booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  recipient_kind TEXT NOT NULL CHECK (recipient_kind IN ('customer','owner')),
  scheduled_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','sent','failed','cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL,
  locked_at INTEGER,
  sent_at INTEGER,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE (booking_id, kind, recipient_kind)
);
CREATE INDEX IF NOT EXISTS idx_notification_due ON notification_jobs (status, next_attempt_at, scheduled_at);
