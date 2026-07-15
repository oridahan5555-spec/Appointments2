import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import auth
import availability
import config
import db
import google_calendar
import mailer


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not config.OWNER_EMAIL:
        raise RuntimeError("OWNER_EMAIL must be configured")
    if config.MAIL_PROVIDER != "mailjet":
        raise RuntimeError("MAIL_PROVIDER must be mailjet")
    yield


app = FastAPI(title="Booking", docs_url=None, redoc_url=None, lifespan=lifespan)


def bool_value(value) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "on"} else 0
    return 0


def clean_text(value: str | None, max_len: int = 500) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:max_len] if value else None


def validate_date_or_400(value: str) -> str:
    try:
        return availability.validate_date(value)
    except ValueError:
        raise HTTPException(400, "תאריך לא תקין") from None


def validate_time_or_400(value: str) -> str:
    try:
        return availability.validate_time(value)
    except ValueError:
        raise HTTPException(400, "שעה לא תקינה") from None


class EmailIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class VerifyIn(EmailIn):
    code: str = Field(pattern=r"^\d{4}$")


class BookingIn(BaseModel):
    service_ids: list[int] = Field(min_length=1, max_length=8)
    date: str
    time: str
    name: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        return validate_date_or_400(value)

    @field_validator("time")
    @classmethod
    def valid_time(cls, value):
        return validate_time_or_400(value)


class StatusIn(BaseModel):
    status: str = Field(pattern=r"^(approved|rejected|cancelled)$")


class ArrivalIn(BaseModel):
    answer: str = Field(pattern=r"^(confirmed|declined)$")


class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str | None = Field(default=None, max_length=80)
    price: int = Field(default=0, ge=0, le=100000)
    duration_minutes: int = Field(ge=5, le=480)
    is_active: bool | int | str = 1
    display_order: int = Field(default=0, ge=0, le=100000)


class HoursIn(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    is_closed: bool | int | str = 0
    open_time: str | None = None
    close_time: str | None = None
    slot_interval_minutes: int = Field(default=15, ge=5, le=240)

    @field_validator("open_time", "close_time")
    @classmethod
    def valid_optional_time(cls, value):
        if value in (None, ""):
            return None
        return validate_time_or_400(value)


class OverrideIn(BaseModel):
    override_date: str
    is_closed: bool | int | str = 0
    open_time: str | None = None
    close_time: str | None = None
    slot_interval_minutes: int | None = Field(default=None, ge=5, le=240)
    internal_note: str | None = Field(default=None, max_length=500)

    @field_validator("override_date")
    @classmethod
    def valid_date(cls, value):
        return validate_date_or_400(value)

    @field_validator("open_time", "close_time")
    @classmethod
    def valid_optional_time(cls, value):
        if value in (None, ""):
            return None
        return validate_time_or_400(value)


class BlockIn(BaseModel):
    blocked_date: str
    blocked_time: str
    duration_minutes: int = Field(default=60, ge=5, le=480)
    internal_note: str | None = Field(default=None, max_length=500)

    @field_validator("blocked_date")
    @classmethod
    def valid_date(cls, value):
        return validate_date_or_400(value)

    @field_validator("blocked_time")
    @classmethod
    def valid_time(cls, value):
        return validate_time_or_400(value)


class CustomerIn(BaseModel):
    internal_note: str | None = Field(default=None, max_length=500)
    is_blocked: bool | int | str = 0


class SettingsIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    address: str | None = Field(default=None, max_length=250)
    phone: str | None = Field(default=None, max_length=32)
    social_url: str | None = Field(default=None, max_length=500)
    waze_url: str | None = Field(default=None, max_length=500)
    cover_image: str | None = Field(default=None, max_length=500)
    profile_image: str | None = Field(default=None, max_length=500)
    preparation_message: str | None = Field(default=None, max_length=500)
    min_lead_minutes: int = Field(ge=0, le=10080)
    max_days_ahead: int = Field(ge=1, le=365)


def clean_public_settings(settings):
    keys = ("name", "description", "address", "phone", "social_url", "waze_url", "cover_image", "profile_image", "preparation_message", "min_lead_minutes", "max_days_ahead")
    return {k: settings[k] for k in keys}


def google_redirect_uri(request: Request) -> str:
    if config.GOOGLE_REDIRECT_URI:
        return config.GOOGLE_REDIRECT_URI
    return str(request.url_for("google_callback"))


@app.middleware("http")
async def owner_guard(request: Request, call_next):
    if request.url.path.startswith("/api/owner/"):
        try:
            auth.require_owner(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.get("/api/business")
def business():
    with db.get_conn() as conn:
        return {"settings": clean_public_settings(db.settings(conn)), "services": db.public_services(conn)}


@app.get("/api/slots")
def slots(date_from: str, date_to: str, duration: int):
    if duration < 1 or duration > 480:
        raise HTTPException(400, "משך לא תקין")
    try:
        with db.get_conn() as conn:
            return {"days": availability.available_slots(conn, date_from, date_to, duration)}
    except ValueError:
        raise HTTPException(400, "טווח תאריכים לא תקין") from None


@app.post("/api/auth/request-code")
def request_code(data: EmailIn, request: Request):
    email = auth.normalize_email(data.email)
    ip = request.client.host if request.client else "unknown"
    ts = db.now()
    with db.get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM otp_requests WHERE email=? AND created_at>?", (email, ts - 3600)).fetchone()[0] >= 3:
            raise HTTPException(429, "יותר מדי בקשות. נסי שוב בעוד שעה.")
        if conn.execute("SELECT COUNT(*) FROM otp_requests WHERE ip=? AND created_at>?", (ip, ts - 3600)).fetchone()[0] >= 10:
            raise HTTPException(429, "יותר מדי בקשות. נסי שוב בעוד שעה.")
        code = f"{secrets.randbelow(10000):04d}"
        conn.execute("INSERT INTO otp_requests (email,ip,created_at) VALUES (?,?,?)", (email, ip, ts))
        conn.execute("INSERT OR REPLACE INTO otp_codes (email,code_hash,expires_at,attempts,created_at) VALUES (?,?,?,?,?)", (email, auth.otp_hash(code), ts + 300, 0, ts))
    status = mailer.send_email(
        email,
        "otp",
        "קוד האימות שלך",
        "הקוד תקף לחמש דקות. אין להעביר אותו לאדם אחר.",
        code=code,
    )
    if not status.startswith("mailjet:2"):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM otp_codes WHERE email=?", (email,))
        raise HTTPException(503, "לא הצלחנו לשלוח את המייל כרגע. נסו שוב בעוד דקה.")
    return {"ok": True}


@app.post("/api/auth/verify")
def verify(data: VerifyIn, response: Response):
    email = auth.normalize_email(data.email)
    bad = HTTPException(400, "הקוד לא נכון או שפג תוקפו. אפשר לשלוח קוד חדש.")
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM otp_codes WHERE email=?", (email,)).fetchone()
        if not row or row["expires_at"] < db.now() or row["attempts"] >= 5:
            raise bad
        conn.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE email=?", (email,))
        import hmac
        if not hmac.compare_digest(row["code_hash"], auth.otp_hash(data.code)):
            raise bad
        conn.execute("DELETE FROM otp_codes WHERE email=?", (email,))
        customer = db.customer_by_email(conn, email)
        role = "owner" if email == config.OWNER_EMAIL else "customer"
        auth.create_session(conn, response, email, role, customer["id"] if customer else None)
        return {"role": role, "is_new": customer is None and role == "customer", "name": customer["name"] if customer else None}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE)
    if token:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (auth.token_hash(token),))
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    session = auth.require_customer(request)
    with db.get_conn() as conn:
        customer = db.customer_by_id(conn, session["customer_id"]) if session["customer_id"] else None
        return {"name": customer["name"] if customer else None, "email": session["email"], "role": session["role"]}


@app.post("/api/bookings")
def create_booking(data: BookingIn, request: Request):
    session = auth.require_customer(request)
    unique_service_ids = list(dict.fromkeys(data.service_ids))
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            services = db.active_services_by_ids(conn, unique_service_ids)
            if len(services) != len(unique_service_ids):
                raise HTTPException(400, "שירות לא תקין")
            duration = sum(x["duration_minutes"] for x in services)
            starts, ends = availability.to_unix(data.date, data.time, duration)
            settings = db.settings(conn)
            if starts < db.now() + settings["min_lead_minutes"] * 60:
                raise HTTPException(409, "המועד כבר לא זמין. בחרי שעה אחרת.")
            if not availability.within_working_hours(conn, data.date, data.time, duration):
                raise HTTPException(409, "השעה לא זמינה. בחרי שעה אחרת.")
            if db.booking_overlap(conn, starts, ends):
                raise HTTPException(409, "השעה נתפסה. בחרי שעה אחרת.")
            if db.block_overlap(conn, starts, ends):
                raise HTTPException(409, "השעה לא זמינה. בחרי שעה אחרת.")
            customer = db.customer_by_email(conn, session["email"])
            if customer and customer["is_blocked"]:
                raise HTTPException(403, "לא ניתן לקבוע תור אונליין. אפשר להתקשר.")
            if not customer:
                if not data.name or len(data.name.strip()) < 2:
                    raise HTTPException(400, "שם מלא נדרש לקביעת תור ראשון.")
                customer_id = db.upsert_customer(conn, session["email"], clean_text(data.name, 80))
                conn.execute("UPDATE sessions SET customer_id=? WHERE token_hash=?", (customer_id, auth.token_hash(request.cookies[auth.COOKIE])))
                customer = db.customer_by_id(conn, customer_id)
            booking_id = db.insert_booking(conn, customer["id"], unique_service_ids, services, data.date, data.time, clean_text(data.notes, 500), starts, ends)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"id": booking_id, "status": "pending"}


@app.get("/api/bookings/mine")
def mine(request: Request):
    session = auth.require_customer(request)
    if not session["customer_id"]:
        return {"bookings": []}
    with db.get_conn() as conn:
        return {"bookings": db.customer_bookings(conn, session["customer_id"])}


@app.post("/api/bookings/{booking_id}/cancel")
def cancel_mine(booking_id: int, request: Request):
    session = auth.require_customer(request)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            booking = db.booking_for_customer(conn, booking_id, session["customer_id"])
            if not booking:
                raise HTTPException(404, "לא נמצא")
            if booking["status"] not in ("pending", "approved"):
                raise HTTPException(400, "אי אפשר לבטל את התור הזה.")
            event_id = booking.get("google_calendar_event_id")
            try:
                if event_id:
                    google_calendar.delete_event(conn, event_id)
                    event_id = None
            except google_calendar.GoogleCalendarError as exc:
                raise HTTPException(502, "Google Calendar לא הצליח למחוק את האירוע. נסי שוב.") from exc
            conn.execute("UPDATE bookings SET status='cancelled',google_calendar_event_id=?,updated_at=? WHERE id=?", (event_id, db.now(), booking_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"ok": True}


@app.post("/api/bookings/{booking_id}/hide")
def hide_mine(booking_id: int, request: Request):
    session = auth.require_customer(request)
    with db.get_conn() as conn:
        booking = db.booking_for_customer(conn, booking_id, session["customer_id"])
        if not booking:
            raise HTTPException(404, "לא נמצא")
        if booking["status"] not in ("cancelled", "rejected"):
            raise HTTPException(400, "אפשר להסתיר רק תור שבוטל או נדחה.")
        conn.execute("UPDATE bookings SET hidden_by_customer=1 WHERE id=?", (booking_id,))
    return {"ok": True}


@app.post("/api/bookings/{booking_id}/arrival")
def arrival_mine(booking_id: int, data: ArrivalIn, request: Request):
    session = auth.require_customer(request)
    with db.get_conn() as conn:
        booking = db.booking_for_customer(conn, booking_id, session["customer_id"])
        if not booking or booking["arrival_status"] != "requested":
            raise HTTPException(404, "לא נמצא")
        conn.execute("UPDATE bookings SET arrival_status=?,updated_at=? WHERE id=?", (data.answer, db.now(), booking_id))
    return {"ok": True}


@app.get("/api/bookings/{booking_id}/ics")
def ics(booking_id: int, request: Request):
    session = auth.require_customer(request)
    with db.get_conn() as conn:
        booking = db.booking_for_customer(conn, booking_id, session["customer_id"])
        if not booking:
            raise HTTPException(404, "לא נמצא")
        settings = db.settings(conn)
    fmt = "%Y%m%dT%H%M%SZ"
    body = "\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Booking//HE",
        "BEGIN:VEVENT",
        f"UID:booking-{booking_id}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{datetime.fromtimestamp(booking['starts_at'], timezone.utc).strftime(fmt)}",
        f"DTEND:{datetime.fromtimestamp(booking['ends_at'], timezone.utc).strftime(fmt)}",
        f"SUMMARY:{settings['name']}",
        "DESCRIPTION:תור",
        "END:VEVENT",
        "END:VCALENDAR",
    ])
    return PlainTextResponse(body, media_type="text/calendar; charset=utf-8")


@app.get("/api/owner/bookings")
def owner_bookings(date_from: str | None = None, date_to: str | None = None, status: str | None = None):
    today = date.today()
    start = validate_date_or_400(date_from) if date_from else today.isoformat()
    end = validate_date_or_400(date_to) if date_to else (today + timedelta(days=14)).isoformat()
    if status and status not in {"pending", "approved", "rejected", "cancelled"}:
        raise HTTPException(400, "סטטוס לא תקין")
    with db.get_conn() as conn:
        return {"bookings": db.owner_bookings(conn, start, end, status)}


@app.get("/api/owner/google/status")
def google_status():
    with db.get_conn() as conn:
        return {
            "oauth_ready": google_calendar.oauth_ready(),
            "connected": google_calendar.is_connected(conn),
            "calendar_id": config.GOOGLE_CALENDAR_ID,
        }


@app.get("/api/owner/google/connect")
def google_connect(request: Request):
    state = secrets.token_urlsafe(32)
    redirect_uri = google_redirect_uri(request)
    with db.get_conn() as conn:
        db.create_oauth_state(conn, "google", state)
    return RedirectResponse(google_calendar.authorization_url(redirect_uri, state), status_code=302)


@app.get("/api/owner/google/callback", name="google_callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"Google OAuth error: {error}")
    if not code or not state:
        raise HTTPException(400, "Google OAuth response is missing code or state")
    with db.get_conn() as conn:
        if not db.consume_oauth_state(conn, "google", state):
            raise HTTPException(400, "Google OAuth state is invalid or expired")
        try:
            token = google_calendar.exchange_code_for_refresh_token(code, google_redirect_uri(request))
        except google_calendar.GoogleCalendarError as exc:
            raise HTTPException(502, "Google Calendar לא הצליח להשלים את החיבור. בדוק OAuth ונסה שוב.") from exc
        db.set_secret(conn, "google_refresh_token", token)
    return RedirectResponse("/owner.html?google=connected", status_code=302)


@app.post("/api/owner/google/disconnect")
def google_disconnect():
    with db.get_conn() as conn:
        db.delete_secret(conn, "google_refresh_token")
    return {"ok": True}


@app.post("/api/owner/bookings/{booking_id}/status")
def owner_status(booking_id: int, data: StatusIn):
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            booking = db.booking_with_customer(conn, booking_id)
            if not booking:
                raise HTTPException(404, "לא נמצא")
            allowed = (booking["status"] == "pending" and data.status in ("approved", "rejected", "cancelled")) or (booking["status"] == "approved" and data.status == "cancelled")
            if not allowed:
                raise HTTPException(400, "מעבר סטטוס לא מותר")
            event_id = booking.get("google_calendar_event_id")
            try:
                if data.status == "approved" and google_calendar.is_connected(conn):
                    event_id = google_calendar.update_event(conn, event_id, booking, db.settings(conn))
                if data.status == "cancelled" and event_id:
                    google_calendar.delete_event(conn, event_id)
                    event_id = None
            except google_calendar.GoogleCalendarError as exc:
                raise HTTPException(502, "Google Calendar לא הצליח לעדכן את היומן. בדוק את הגדרות OAuth ונסה שוב.") from exc
            conn.execute("UPDATE bookings SET status=?,google_calendar_event_id=?,updated_at=? WHERE id=?", (data.status, event_id, db.now(), booking_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"ok": True}


@app.post("/api/owner/bookings/{booking_id}/request-arrival")
def request_arrival(booking_id: int):
    with db.get_conn() as conn:
        cur = conn.execute("UPDATE bookings SET arrival_status='requested',updated_at=? WHERE id=? AND status='approved'", (db.now(), booking_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "לא נמצא תור מאושר")
    return {"ok": True}


@app.post("/api/owner/bookings/{booking_id}/no-show")
def no_show(booking_id: int):
    with db.get_conn() as conn:
        booking = db.rowdict(conn.execute("SELECT customer_id FROM bookings WHERE id=? AND status='approved'", (booking_id,)).fetchone())
        if not booking:
            raise HTTPException(404, "לא נמצא")
        conn.execute("UPDATE bookings SET arrival_status='no_show',updated_at=? WHERE id=?", (db.now(), booking_id))
        conn.execute("UPDATE customers SET no_show_count=no_show_count+1 WHERE id=?", (booking["customer_id"],))
    return {"ok": True}


@app.get("/api/owner/services")
def services_get():
    with db.get_conn() as conn:
        return {"services": db.all_services(conn)}


@app.post("/api/owner/services")
def services_add(item: ServiceIn):
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO services (name,category,price,duration_minutes,is_active,display_order) VALUES (?,?,?,?,?,?)", (clean_text(item.name, 100), clean_text(item.category, 80), item.price, item.duration_minutes, bool_value(item.is_active), item.display_order))
        return {"id": cur.lastrowid}


@app.put("/api/owner/services/{service_id}")
def services_put(service_id: int, item: ServiceIn):
    with db.get_conn() as conn:
        cur = conn.execute("UPDATE services SET name=?,category=?,price=?,duration_minutes=?,is_active=?,display_order=? WHERE id=?", (clean_text(item.name, 100), clean_text(item.category, 80), item.price, item.duration_minutes, bool_value(item.is_active), item.display_order, service_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "לא נמצא")
    return {"ok": True}


@app.delete("/api/owner/services/{service_id}")
def services_delete(service_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM services WHERE id=?", (service_id,))
    return {"ok": True}


@app.get("/api/owner/hours")
def hours_get():
    with db.get_conn() as conn:
        return {"hours": db.working_hours(conn)}


@app.put("/api/owner/hours")
def hours_put(items: list[HoursIn]):
    if len(items) != 7:
        raise HTTPException(400, "יש לשלוח 7 ימים")
    with db.get_conn() as conn:
        for item in items:
            if not bool_value(item.is_closed) and (not item.open_time or not item.close_time or availability.parse_minutes(item.open_time) >= availability.parse_minutes(item.close_time)):
                raise HTTPException(400, "שעות פתיחה לא תקינות")
            conn.execute("UPDATE working_hours SET is_closed=?,open_time=?,close_time=?,slot_interval_minutes=? WHERE day_of_week=?", (bool_value(item.is_closed), item.open_time, item.close_time, item.slot_interval_minutes, item.day_of_week))
    return {"ok": True}


@app.get("/api/owner/overrides")
def overrides_get(date_from: str = "0000-01-01", date_to: str = "9999-12-31"):
    with db.get_conn() as conn:
        return {"overrides": db.overrides_between(conn, date_from, date_to)}


@app.post("/api/owner/overrides")
def overrides_post(item: OverrideIn):
    if not bool_value(item.is_closed) and (not item.open_time or not item.close_time):
        raise HTTPException(400, "נדרשות שעות פתיחה וסגירה")
    with db.get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO date_overrides VALUES (?,?,?,?,?,?)", (item.override_date, bool_value(item.is_closed), item.open_time, item.close_time, item.slot_interval_minutes, clean_text(item.internal_note, 500)))
    return {"ok": True}


@app.delete("/api/owner/overrides/{day}")
def overrides_delete(day: str):
    validate_date_or_400(day)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM date_overrides WHERE override_date=?", (day,))
    return {"ok": True}


@app.get("/api/owner/blocks")
def blocks_get(date_from: str = "0000-01-01", date_to: str = "9999-12-31"):
    with db.get_conn() as conn:
        return {"blocks": db.blocks_between(conn, date_from, date_to)}


@app.post("/api/owner/blocks")
def blocks_post(item: BlockIn):
    starts, ends = availability.to_unix(item.blocked_date, item.blocked_time, item.duration_minutes)
    with db.get_conn() as conn:
        cur = conn.execute("INSERT INTO blocked_slots (blocked_date,blocked_time,duration_minutes,internal_note,starts_at,ends_at) VALUES (?,?,?,?,?,?)", (item.blocked_date, item.blocked_time, item.duration_minutes, clean_text(item.internal_note, 500), starts, ends))
        return {"id": cur.lastrowid}


@app.delete("/api/owner/blocks/{block_id}")
def blocks_delete(block_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM blocked_slots WHERE id=?", (block_id,))
    return {"ok": True}


@app.get("/api/owner/customers")
def customers_get():
    with db.get_conn() as conn:
        return {"customers": [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY created_at DESC")]}


@app.put("/api/owner/customers/{customer_id}")
def customers_put(customer_id: int, item: CustomerIn):
    with db.get_conn() as conn:
        conn.execute("UPDATE customers SET internal_note=?,is_blocked=? WHERE id=?", (clean_text(item.internal_note, 500), bool_value(item.is_blocked), customer_id))
    return {"ok": True}


@app.get("/api/owner/settings")
def settings_get():
    with db.get_conn() as conn:
        return db.settings(conn)


@app.put("/api/owner/settings")
def settings_put(item: SettingsIn):
    fields = ["name", "description", "address", "phone", "social_url", "waze_url", "cover_image", "profile_image", "preparation_message", "min_lead_minutes", "max_days_ahead"]
    values = [getattr(item, f) for f in fields]
    with db.get_conn() as conn:
        conn.execute("UPDATE settings SET " + ",".join(f"{f}=?" for f in fields) + " WHERE id=1", values)
    return {"ok": True}


@app.post("/api/owner/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 3_000_000:
        raise HTTPException(400, "קובץ גדול מדי")
    kinds = [(b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"RIFF", ".webp")]
    ext = next((e for magic, e in kinds if data.startswith(magic)), None)
    if not ext:
        raise HTTPException(400, "רק JPEG, PNG או WebP")
    name = secrets.token_hex(16) + ext
    (config.UPLOAD_DIR / name).write_bytes(data)
    return {"url": f"/uploads/{name}"}


@app.get("/uploads/{name}")
def uploaded(name: str):
    if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|webp)", name):
        raise HTTPException(404)
    return FileResponse(config.UPLOAD_DIR / name)


app.mount("/", StaticFiles(directory=config.BASE_DIR / "static", html=True), name="static")
