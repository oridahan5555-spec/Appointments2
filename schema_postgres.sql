CREATE TABLE IF NOT EXISTS settings (
  id SMALLINT PRIMARY KEY CHECK (id = 1),
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
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,
  price INTEGER NOT NULL DEFAULT 0 CHECK (price >= 0),
  duration_minutes INTEGER NOT NULL CHECK (duration_minutes BETWEEN 5 AND 480),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS working_hours (
  day_of_week INTEGER PRIMARY KEY CHECK (day_of_week BETWEEN 0 AND 6),
  is_closed INTEGER NOT NULL DEFAULT 0 CHECK (is_closed IN (0,1)),
  open_time TEXT,
  close_time TEXT,
  slot_interval_minutes INTEGER NOT NULL DEFAULT 15
);

CREATE TABLE IF NOT EXISTS date_overrides (
  override_date TEXT PRIMARY KEY,
  is_closed INTEGER NOT NULL DEFAULT 0 CHECK (is_closed IN (0,1)),
  open_time TEXT,
  close_time TEXT,
  slot_interval_minutes INTEGER,
  internal_note TEXT
);

CREATE TABLE IF NOT EXISTS blocked_slots (
  id BIGSERIAL PRIMARY KEY,
  blocked_date TEXT NOT NULL,
  blocked_time TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL DEFAULT 60 CHECK (duration_minutes BETWEEN 5 AND 480),
  internal_note TEXT,
  starts_at BIGINT NOT NULL,
  ends_at BIGINT NOT NULL CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS idx_blocked_range ON blocked_slots (starts_at, ends_at);

CREATE TABLE IF NOT EXISTS customers (
  id BIGSERIAL PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  internal_note TEXT,
  is_blocked INTEGER NOT NULL DEFAULT 0 CHECK (is_blocked IN (0,1)),
  no_show_count INTEGER NOT NULL DEFAULT 0 CHECK (no_show_count >= 0),
  created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
  id BIGSERIAL PRIMARY KEY,
  customer_id BIGINT NOT NULL REFERENCES customers(id),
  service_ids TEXT NOT NULL,
  services_snapshot TEXT NOT NULL,
  booking_date TEXT NOT NULL,
  booking_time TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL CHECK (duration_minutes BETWEEN 1 AND 480),
  price INTEGER NOT NULL CHECK (price >= 0),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled')),
  arrival_status TEXT CHECK (arrival_status IN ('requested','confirmed','declined','no_show')),
  notes TEXT,
  starts_at BIGINT NOT NULL,
  ends_at BIGINT NOT NULL CHECK (ends_at > starts_at),
  google_calendar_event_id TEXT,
  calendar_sync_status TEXT NOT NULL DEFAULT 'not_connected' CHECK (calendar_sync_status IN ('not_connected','pending','synced','failed')),
  calendar_sync_error TEXT,
  calendar_synced_at BIGINT,
  hidden_by_customer INTEGER NOT NULL DEFAULT 0 CHECK (hidden_by_customer IN (0,1)),
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS google_calendar_event_id TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS calendar_sync_status TEXT NOT NULL DEFAULT 'not_connected';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS calendar_sync_error TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS calendar_synced_at BIGINT;
CREATE INDEX IF NOT EXISTS idx_bookings_range ON bookings (starts_at, ends_at, status);
CREATE INDEX IF NOT EXISTS idx_bookings_cust ON bookings (customer_id);
CREATE INDEX IF NOT EXISTS idx_bookings_date_status ON bookings (booking_date, status);
CREATE INDEX IF NOT EXISTS idx_bookings_calendar_sync ON bookings (calendar_sync_status, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_google_event ON bookings (google_calendar_event_id) WHERE google_calendar_event_id IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bookings_no_time_overlap') THEN
    ALTER TABLE bookings ADD CONSTRAINT bookings_no_time_overlap
      EXCLUDE USING gist (int8range(starts_at, ends_at, '[)') WITH &&)
      WHERE (status IN ('pending','approved'));
  END IF;
END $$;

CREATE OR REPLACE FUNCTION validate_booking_status_transition()
RETURNS trigger AS $$
BEGIN
  IF NEW.status = OLD.status
     OR (OLD.status = 'pending' AND NEW.status IN ('approved','rejected','cancelled'))
     OR (OLD.status = 'approved' AND NEW.status = 'cancelled') THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'invalid_booking_status_transition' USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_bookings_status_transition ON bookings;
CREATE TRIGGER trg_bookings_status_transition
BEFORE UPDATE OF status ON bookings
FOR EACH ROW EXECUTE FUNCTION validate_booking_status_transition();

CREATE TABLE IF NOT EXISTS otp_codes (
  email TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'customer' CHECK (purpose IN ('customer','owner')),
  expires_at BIGINT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL
);
ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'customer';

CREATE TABLE IF NOT EXISTS otp_requests (
  id BIGSERIAL PRIMARY KEY,
  email TEXT NOT NULL,
  ip TEXT NOT NULL,
  created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_otp_req_email ON otp_requests (email, created_at);
CREATE INDEX IF NOT EXISTS idx_otp_req_ip ON otp_requests (ip, created_at);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  csrf_token TEXT,
  customer_id BIGINT REFERENCES customers(id),
  email TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('customer','owner')),
  expires_at BIGINT NOT NULL,
  created_at BIGINT NOT NULL
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS csrf_token TEXT;
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS email_log (
  id BIGSERIAL PRIMARY KEY,
  recipient TEXT NOT NULL,
  template TEXT NOT NULL,
  provider_status TEXT NOT NULL,
  sent_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_secrets (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  session_hash TEXT,
  redirect_uri TEXT,
  expires_at BIGINT NOT NULL,
  created_at BIGINT NOT NULL
);
ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS session_hash TEXT;
ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS redirect_uri TEXT;

CREATE TABLE IF NOT EXISTS notification_jobs (
  id BIGSERIAL PRIMARY KEY,
  booking_id BIGINT NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  recipient_kind TEXT NOT NULL CHECK (recipient_kind IN ('customer','owner')),
  scheduled_at BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','sent','failed','cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at BIGINT NOT NULL,
  locked_at BIGINT,
  sent_at BIGINT,
  last_error TEXT,
  created_at BIGINT NOT NULL,
  UNIQUE (booking_id, kind, recipient_kind)
);
CREATE INDEX IF NOT EXISTS idx_notification_due ON notification_jobs (status, next_attempt_at, scheduled_at);

CREATE TABLE IF NOT EXISTS rate_limit_events (
  id BIGSERIAL PRIMARY KEY,
  scope TEXT NOT NULL,
  identity_hash TEXT NOT NULL,
  created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit_events (scope, identity_hash, created_at);
