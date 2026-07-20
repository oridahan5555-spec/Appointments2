import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

import auth
import availability
import calendar_sync
import config
import db
import google_calendar
import mailer
import notifications
import storage
import inspect
import importlib.metadata
import traceback

logger = logging.getLogger("booking")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _app.state.startup_error = None
    try:
        config.validate_runtime_config()
        db.init_db()
        if not config.VERCEL:
            config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.exception("Application startup failed")
        _app.state.startup_error = exc
    yield


app = FastAPI(title="Booking", docs_url=None, redoc_url=None, lifespan=lifespan)
app.state.startup_error = None
app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)


@app.get("/api/debug/blob")
async def debug_blob(request: Request):
    info: dict = {}
    # Environment flags
    try:
        is_vercel = bool(config.VERCEL)
    except Exception:
        is_vercel = bool(config.env("VERCEL"))
    info["VERCEL"] = is_vercel

    has_blob_token = bool(config.env("BLOB_READ_WRITE_TOKEN") or config.env("VERCEL_BLOB_READ_WRITE_TOKEN") or getattr(config, "BLOB_READ_WRITE_TOKEN", None))
    info["BLOB_READ_WRITE_TOKEN_exists"] = bool(has_blob_token)

    # vercel package version
    try:
        vercel_version = importlib.metadata.version("vercel")
    except Exception as exc:
        vercel_version = f"error: {exc!r}"
    info["vercel_version"] = vercel_version

    # AsyncBlobClient creation
    from vercel.blob import AsyncBlobClient

    try:
        # Try instantiating without passing a token (SDK will resolve auth)
        client_instance = AsyncBlobClient()
        info["async_blob_client_created"] = True
    except Exception as exc:
        info["async_blob_client_created"] = False
        info["async_blob_client_error"] = {
            "type": type(exc).__name__,
            "repr": repr(exc),
            "traceback": traceback.format_exc(),
        }

    # Signature of put
    try:
        sig = str(inspect.signature(AsyncBlobClient.put))
    except Exception as exc:
        sig = f"error: {exc!r}"
    info["async_blob_client_put_signature"] = sig

    # Attempt upload of a tiny file and capture any error details
    upload_info: dict = {}
    try:
        async with AsyncBlobClient() as client:
            result = await client.put(
                "debug/hello.txt",
                b"hello\n",
                access="private",
                content_type="text/plain",
                add_random_suffix=False,
            )
        upload_info["result"] = {
            "repr": repr(result),
            "url": getattr(result, "url", None),
        }
    except Exception as exc:
        # Collect as much info as possible from the exception
        err: dict = {
            "type": type(exc).__name__,
            "repr": repr(exc),
            "str": str(exc),
            "traceback": traceback.format_exc(),
        }
        # Common attributes to check for HTTP error details
        for attr in ("status_code", "status", "response", "body", "text", "content", "raw"):
            try:
                val = getattr(exc, attr, None)
            except Exception:
                val = None
            if val is None:
                continue
            try:
                # If it's a requests/urllib3 response-like object
                if hasattr(val, "text"):
                    err[attr] = val.text
                else:
                    err[attr] = val
            except Exception:
                try:
                    err[attr] = repr(val)
                except Exception:
                    err[attr] = "<unrepresentable>"

        upload_info["error"] = err

    info["upload_attempt"] = upload_info
    return info


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


def safe_url(value: str | None, *, allow_upload: bool = False) -> str | None:
    value = clean_text(value, 500)
    if value is None:
        return None
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("URL contains control characters")
    if allow_upload and re.fullmatch(r"/uploads/[a-f0-9]{32}\.(jpg|png|webp)", value):
        return value
    try:
        parsed = urlparse(value)
    except ValueError:
        raise ValueError("invalid URL") from None
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("only HTTPS URLs are allowed")
    return value


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


def enforce_request_limit(
    scope: str,
    identity: str,
    limit: int,
    window_seconds: int,
) -> None:
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        allowed = db.enforce_rate_limit(
            conn,
            scope,
            auth.rate_identity(scope, identity),
            limit,
            window_seconds,
        )
    if not allowed:
        raise HTTPException(429, "יותר מדי בקשות. נסו שוב מאוחר יותר.")


def is_booking_overlap_error(exc: Exception) -> bool:
    return "booking_overlap" in str(exc) or getattr(exc, "sqlstate", None) == "23P01"


def issue_otp(email: str, ip: str, purpose: str):
    ts = db.now()
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        email_allowed = db.enforce_rate_limit(
            conn, "otp-request-email", auth.rate_identity("email", email), 3, 3600
        )
        ip_allowed = db.enforce_rate_limit(
            conn, "otp-request-ip", auth.rate_identity("ip", ip), 10, 3600
        )
        if not email_allowed or not ip_allowed:
            raise HTTPException(429, "יותר מדי בקשות. נסו שוב מאוחר יותר.")
        code = f"{secrets.randbelow(1_000_000):06d}"
        conn.execute(
            "INSERT INTO otp_requests (email,ip,created_at) VALUES (?,?,?)",
            (email, ip, ts),
        )
        conn.execute("DELETE FROM otp_requests WHERE created_at<?", (ts - 86400,))
        conn.execute(
            "INSERT INTO otp_codes (email,code_hash,purpose,expires_at,attempts,created_at) "
            "VALUES (?,?,?,?,0,?) ON CONFLICT (email) DO UPDATE SET "
            "code_hash=excluded.code_hash,purpose=excluded.purpose,"
            "expires_at=excluded.expires_at,attempts=0,created_at=excluded.created_at",
            (email, auth.otp_hash(email, purpose, code), purpose, ts + 300, ts),
        )
    status = mailer.send_email(
        email,
        "otp",
        "קוד האימות שלך",
        "הקוד תקף לחמש דקות. אין להעביר אותו לאדם אחר.",
        code=code,
    )
    if not status.startswith("mailjet:2"):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM otp_codes WHERE email=? AND purpose=?", (email, purpose))
        raise HTTPException(503, "לא הצלחנו לשלוח את המייל כרגע. נסו שוב בעוד דקה.")


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailIn(ApiModel):
    email: str = Field(min_length=3, max_length=254)


class VerifyIn(EmailIn):
    code: str = Field(pattern=r"^\d{6}$")


class BookingIn(ApiModel):
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


class StatusIn(ApiModel):
    status: str = Field(pattern=r"^(approved|rejected|cancelled)$")


class RescheduleIn(ApiModel):
    date: str
    time: str

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        return validate_date_or_400(value)

    @field_validator("time")
    @classmethod
    def valid_time(cls, value):
        return validate_time_or_400(value)


class ArrivalIn(ApiModel):
    answer: str = Field(pattern=r"^(confirmed|declined)$")


class ServiceIn(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    category: str | None = Field(default=None, max_length=80)
    price: int = Field(default=0, ge=0, le=100000)
    duration_minutes: int = Field(ge=5, le=480)
    is_active: bool = True
    display_order: int = Field(default=0, ge=0, le=100000)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if not value.strip():
            raise ValueError("service name is required")
        return value.strip()


class HoursIn(ApiModel):
    day_of_week: int = Field(ge=0, le=6)
    is_closed: bool = False
    open_time: str | None = None
    close_time: str | None = None
    slot_interval_minutes: int = Field(default=15, ge=5, le=240)

    @field_validator("open_time", "close_time")
    @classmethod
    def valid_optional_time(cls, value):
        if value in (None, ""):
            return None
        return validate_time_or_400(value)


class OverrideIn(ApiModel):
    override_date: str
    is_closed: bool = False
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


class BlockIn(ApiModel):
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


class CustomerIn(ApiModel):
    internal_note: str | None = Field(default=None, max_length=500)
    is_blocked: bool = False


class SettingsIn(ApiModel):
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

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if not value.strip():
            raise ValueError("business name is required")
        return value.strip()

    @field_validator("social_url", "waze_url")
    @classmethod
    def valid_link(cls, value):
        return safe_url(value)

    @field_validator("cover_image", "profile_image")
    @classmethod
    def valid_image_link(cls, value):
        return safe_url(value, allow_upload=True)

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value):
        value = clean_text(value, 32)
        if value and not re.fullmatch(r"[+0-9()\-\s]{5,32}", value):
            raise ValueError("invalid phone")
        return value


def clean_public_settings(settings):
    keys = (
        "name",
        "description",
        "address",
        "phone",
        "social_url",
        "waze_url",
        "cover_image",
        "profile_image",
        "preparation_message",
        "min_lead_minutes",
        "max_days_ahead",
    )
    return {k: settings[k] for k in keys}


def google_redirect_uri(request: Request) -> str:
    if config.GOOGLE_REDIRECT_URI:
        return config.GOOGLE_REDIRECT_URI
    if config.PUBLIC_BASE_URL:
        return f"{config.PUBLIC_BASE_URL}/api/owner/google/callback"
    if request.url.hostname in {"localhost", "127.0.0.1", "testserver"}:
        return str(request.url_for("google_callback"))
    raise HTTPException(503, "כתובת החזרה של Google אינה מוגדרת")


@app.middleware("http")
async def security_guard(request: Request, call_next):
    request.state.request_id = uuid4().hex
    public_unsafe_routes = {
        "/api/auth/request-code",
        "/api/auth/request-owner-code",
        "/api/auth/verify",
        "/api/auth/verify-owner",
    }
    startup_error = getattr(request.app.state, "startup_error", None)
    if startup_error and request.url.path.startswith("/api/"):
        return JSONResponse(
            {"detail": f"Service unavailable: {startup_error}"},
            status_code=503,
        )
    try:
        session = None
        if request.url.path.startswith("/api/owner/"):
            session = auth.require_owner(request)
        if (
            request.method.upper() not in auth.SAFE_METHODS
            and request.url.path.startswith("/api/")
            and request.url.path not in public_unsafe_routes
        ):
            auth.require_csrf(request, session)
        response = await call_next(request)
    except HTTPException as exc:
        response = JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "style-src-attr 'unsafe-inline'; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["X-Request-ID"] = request.state.request_id
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if config.IS_PRODUCTION or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, _exc: RequestValidationError):
    return JSONResponse({"detail": "אחד הפרטים שנשלחו אינו תקין"}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled request error request_id=%s path=%s type=%s",
        getattr(request.state, "request_id", "unknown"),
        request.url.path,
        type(exc).__name__,
    )
    return JSONResponse(
        {"detail": "אירעה שגיאה זמנית. נסו שוב בעוד רגע."},
        status_code=500,
    )


@app.get("/api/business")
def business():
    with db.get_conn() as conn:
        return {
            "settings": clean_public_settings(db.settings(conn)),
            "services": db.public_services(conn),
        }


@app.get("/api/slots")
def slots(date_from: str, date_to: str, duration: int, request: Request):
    enforce_request_limit("availability-ip", auth.client_ip(request), 120, 60)
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
    if email == config.OWNER_EMAIL:
        return {"ok": True}
    issue_otp(email, auth.client_ip(request), "customer")
    return {"ok": True}


@app.post("/api/auth/request-owner-code")
def request_owner_code(data: EmailIn, request: Request):
    email = auth.normalize_email(data.email)
    if email != config.OWNER_EMAIL:
        return {"ok": True}
    issue_otp(email, auth.client_ip(request), "owner")
    return {"ok": True}


@app.post("/api/auth/verify")
def verify(data: VerifyIn, request: Request, response: Response):
    return verify_otp(data, request, response, "customer")


@app.post("/api/auth/verify-owner")
def verify_owner(data: VerifyIn, request: Request, response: Response):
    return verify_otp(data, request, response, "owner")


def verify_otp(data: VerifyIn, request: Request, response: Response, purpose: str):
    email = auth.normalize_email(data.email)
    if purpose == "owner" and email != config.OWNER_EMAIL:
        raise HTTPException(400, "הקוד לא נכון או שפג תוקפו. אפשר לשלוח קוד חדש.")
    if purpose == "customer" and email == config.OWNER_EMAIL:
        raise HTTPException(400, "הקוד לא נכון או שפג תוקפו. אפשר לשלוח קוד חדש.")
    bad = HTTPException(400, "הקוד לא נכון או שפג תוקפו. אפשר לשלוח קוד חדש.")
    ip = auth.client_ip(request)
    failure: HTTPException | None = None
    result = None
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        email_allowed = db.enforce_rate_limit(
            conn, "otp-verify-email", auth.rate_identity("email", email), 10, 3600
        )
        ip_allowed = db.enforce_rate_limit(
            conn, "otp-verify-ip", auth.rate_identity("ip", ip), 30, 3600
        )
        if not email_allowed or not ip_allowed:
            failure = HTTPException(429, "יותר מדי ניסיונות אימות. נסו שוב מאוחר יותר.")
        else:
            otp_sql = "SELECT * FROM otp_codes WHERE email=? AND purpose=?"
            if conn.postgres:
                otp_sql += " FOR UPDATE"
            row = conn.execute(otp_sql, (email, purpose)).fetchone()
            if not row or row["expires_at"] < db.now() or row["attempts"] >= 5:
                if row:
                    conn.execute("DELETE FROM otp_codes WHERE email=?", (email,))
                failure = bad
            else:
                conn.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE email=?", (email,))
                import hmac

                if not hmac.compare_digest(
                    row["code_hash"], auth.otp_hash(email, purpose, data.code)
                ):
                    failure = bad
                else:
                    conn.execute("DELETE FROM otp_codes WHERE email=?", (email,))
                    customer = db.customer_by_email(conn, email)
                    role = "owner" if purpose == "owner" else "customer"
                    csrf_token = auth.create_session(
                        conn,
                        response,
                        email,
                        role,
                        customer["id"] if customer else None,
                        request.cookies.get(auth.COOKIE),
                    )
                    result = {
                        "role": role,
                        "is_new": customer is None and role == "customer",
                        "name": customer["name"] if customer else None,
                        "csrf_token": csrf_token,
                    }
    if failure:
        raise failure
    return result


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE)
    if token:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (auth.token_hash(token),))
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/session")
def session_status(request: Request):
    with db.get_conn() as conn:
        session = auth.session_from_request(conn, request)
        if not session:
            return {"authenticated": False}
        customer = (
            db.customer_by_id(conn, session["customer_id"]) if session["customer_id"] else None
        )
    return {
        "authenticated": True,
        "name": customer["name"] if customer else None,
        "email": session["email"],
        "role": session["role"],
        "csrf_token": session["csrf_token"],
    }


@app.get("/api/me")
def me(request: Request):
    session = auth.require_customer(request)
    with db.get_conn() as conn:
        customer = (
            db.customer_by_id(conn, session["customer_id"]) if session["customer_id"] else None
        )
        return {
            "name": customer["name"] if customer else None,
            "email": session["email"],
            "role": session["role"],
            "csrf_token": session["csrf_token"],
        }


@app.post("/api/bookings")
def create_booking(data: BookingIn, request: Request):
    session = auth.require_customer(request)
    enforce_request_limit("booking-create-session", session["token_hash"], 8, 3600)
    enforce_request_limit("booking-create-ip", auth.client_ip(request), 20, 3600)
    unique_service_ids = list(dict.fromkeys(data.service_ids))
    try:
        with db.get_conn() as conn, db.transaction(conn, immediate=True):
            services = db.active_services_by_ids(conn, unique_service_ids)
            if len(services) != len(unique_service_ids):
                raise HTTPException(400, "שירות לא תקין")
            duration = sum(x["duration_minutes"] for x in services)
            if duration < 5 or duration > 480:
                raise HTTPException(400, "משך השירותים אינו תקין")
            try:
                starts, ends = availability.to_unix(data.date, data.time, duration)
            except ValueError:
                raise HTTPException(400, "המועד אינו תקין") from None
            settings = db.settings(conn)
            if starts < db.now() + settings["min_lead_minutes"] * 60:
                raise HTTPException(409, "המועד כבר לא זמין. בחרי שעה אחרת.")
            if date.fromisoformat(data.date) > availability.local_today() + timedelta(
                days=settings["max_days_ahead"]
            ):
                raise HTTPException(409, "המועד רחוק מדי. בחרי תאריך קרוב יותר.")
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
                customer_id = db.upsert_customer(conn, session["email"], data.name.strip()[:80])
                conn.execute(
                    "UPDATE sessions SET customer_id=? WHERE token_hash=?",
                    (customer_id, session["token_hash"]),
                )
                customer = db.customer_by_id(conn, customer_id)
            booking_id = db.insert_booking(
                conn,
                customer["id"],
                unique_service_ids,
                services,
                data.date,
                data.time,
                clean_text(data.notes, 500),
                starts,
                ends,
            )
            notifications.queue_booking_created(conn, booking_id)
    except Exception as exc:
        if is_booking_overlap_error(exc):
            raise HTTPException(409, "השעה נתפסה. בחרי שעה אחרת.") from None
        raise

    try:
        notifications.process_due_jobs(limit=4, booking_id=booking_id)
    except Exception:
        logger.exception("Immediate booking email processing failed booking_id=%s", booking_id)
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
    enforce_request_limit("booking-cancel", session["token_hash"], 20, 3600)
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        booking = db.booking_for_customer(conn, booking_id, session["customer_id"])
        if not booking:
            raise HTTPException(404, "לא נמצא")
        if booking["status"] not in ("pending", "approved"):
            raise HTTPException(400, "אי אפשר לבטל את התור הזה.")
        sync_status = "pending" if booking.get("google_calendar_event_id") else "synced"
        conn.execute(
            "UPDATE bookings SET status='cancelled',calendar_sync_status=?,"
            "calendar_sync_error=NULL,updated_at=? WHERE id=?",
            (sync_status, db.now(), booking_id),
        )
        notifications.queue_booking_cancelled(
            conn, booking_id, notify_customer=True, notify_owner=True
        )
    sync_result = calendar_sync.sync_booking(booking_id)
    try:
        notifications.process_due_jobs(limit=4, booking_id=booking_id)
    except Exception:
        logger.exception("Cancellation email processing failed booking_id=%s", booking_id)
    return {"ok": True, "calendar_synced": sync_result["synced"], "warning": sync_result["warning"]}


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
        if (
            not booking
            or booking["status"] != "approved"
            or booking["arrival_status"] != "requested"
        ):
            raise HTTPException(404, "לא נמצא")
        conn.execute(
            "UPDATE bookings SET arrival_status=?,updated_at=? WHERE id=?",
            (data.answer, db.now(), booking_id),
        )
    return {"ok": True}


@app.get("/api/bookings/{booking_id}/ics")
def ics(booking_id: int, request: Request):
    session = auth.require_customer(request)
    with db.get_conn() as conn:
        booking = db.booking_for_customer(conn, booking_id, session["customer_id"])
        if not booking:
            raise HTTPException(404, "לא נמצא")
        settings = db.settings(conn)
    return PlainTextResponse(
        notifications.calendar_file(booking, settings),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="booking-{booking_id}.ics"'},
    )


@app.get("/api/owner/bookings")
def owner_bookings(
    date_from: str | None = None, date_to: str | None = None, status: str | None = None
):
    today = availability.local_today()
    start = validate_date_or_400(date_from) if date_from else today.isoformat()
    end = validate_date_or_400(date_to) if date_to else (today + timedelta(days=14)).isoformat()
    if (
        date.fromisoformat(end) < date.fromisoformat(start)
        or (date.fromisoformat(end) - date.fromisoformat(start)).days > 366
    ):
        raise HTTPException(400, "טווח התאריכים אינו תקין")
    if status and status not in {"pending", "approved", "rejected", "cancelled"}:
        raise HTTPException(400, "סטטוס לא תקין")
    with db.get_conn() as conn:
        return {"bookings": db.owner_bookings(conn, start, end, status)}


@app.get("/api/owner/google/status")
def google_status():
    with db.get_conn() as conn:
        failed = conn.execute(
            "SELECT COUNT(*) AS count FROM bookings WHERE calendar_sync_status='failed'"
        ).fetchone()["count"]
        return {
            "oauth_ready": google_calendar.oauth_ready(),
            "connected": google_calendar.is_connected(conn),
            "calendar_id": config.GOOGLE_CALENDAR_ID,
            "failed_syncs": failed,
        }


@app.post("/api/owner/google/connect")
def google_connect(request: Request):
    session = auth.require_owner(request)
    enforce_request_limit("google-oauth-start", session["token_hash"], 10, 3600)
    state = secrets.token_urlsafe(32)
    redirect_uri = google_redirect_uri(request)
    with db.get_conn() as conn:
        db.create_oauth_state(conn, "google", state, session["token_hash"], redirect_uri)
    return {"authorization_url": google_calendar.authorization_url(redirect_uri, state)}


@app.get("/api/owner/google/callback", name="google_callback")
def google_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
):
    if error:
        raise HTTPException(400, "החיבור ל-Google בוטל או נכשל")
    if not code or not state or len(code) > 4096 or len(state) > 256:
        raise HTTPException(400, "תגובת Google אינה תקינה")
    session = auth.require_owner(request)
    with db.get_conn() as conn:
        oauth_state = db.consume_oauth_state(conn, "google", state, session["token_hash"])
        if not oauth_state:
            raise HTTPException(400, "בקשת החיבור ל-Google פגה או כבר נוצלה")
        try:
            token = google_calendar.exchange_code_for_refresh_token(
                code, oauth_state["redirect_uri"]
            )
        except google_calendar.GoogleCalendarError as exc:
            raise HTTPException(
                502, "Google Calendar לא הצליח להשלים את החיבור. בדוק OAuth ונסה שוב."
            ) from exc
        google_calendar.store_refresh_token(conn, token)
    return RedirectResponse("/owner.html?google=connected", status_code=302)


@app.post("/api/owner/google/disconnect")
def google_disconnect():
    with db.get_conn() as conn:
        revoked = google_calendar.revoke_and_disconnect(conn)
    return {"ok": True, "revoked": revoked}


@app.post("/api/owner/bookings/{booking_id}/status")
def owner_status(booking_id: int, data: StatusIn):
    already_applied = False
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        booking = db.booking_with_customer(conn, booking_id, for_update=True)
        if not booking:
            raise HTTPException(404, "לא נמצא")
        if booking["status"] == data.status:
            already_applied = True
        else:
            allowed = (
                booking["status"] == "pending"
                and data.status in ("approved", "rejected", "cancelled")
            ) or (booking["status"] == "approved" and data.status == "cancelled")
            if not allowed:
                raise HTTPException(400, "מעבר סטטוס לא מותר")
            connected = google_calendar.is_connected(conn)
            if data.status == "approved":
                sync_status = "pending" if connected else "not_connected"
            elif data.status == "cancelled" and booking.get("google_calendar_event_id"):
                sync_status = "pending"
            else:
                sync_status = "synced"
            conn.execute(
                "UPDATE bookings SET status=?,calendar_sync_status=?,"
                "calendar_sync_error=NULL,updated_at=? WHERE id=?",
                (data.status, sync_status, db.now(), booking_id),
            )
            booking["status"] = data.status
            if data.status == "approved":
                notifications.queue_booking_approved(conn, booking)
            elif data.status == "rejected":
                notifications.queue_booking_rejected(conn, booking_id)
            else:
                notifications.queue_booking_cancelled(
                    conn, booking_id, notify_customer=True, notify_owner=False
                )

    sync_result = {"synced": False, "warning": None}
    if data.status in {"approved", "cancelled"}:
        sync_result = calendar_sync.sync_booking(booking_id)
    try:
        notifications.process_due_jobs(limit=6, booking_id=booking_id)
    except Exception:
        logger.exception("Status email processing failed booking_id=%s", booking_id)
    return {
        "ok": True,
        "already_applied": already_applied,
        "calendar_synced": sync_result["synced"],
        "warning": sync_result["warning"],
    }


@app.put("/api/owner/bookings/{booking_id}/schedule")
def owner_reschedule(booking_id: int, data: RescheduleIn, request: Request):
    session = auth.require_owner(request)
    enforce_request_limit("booking-reschedule", session["token_hash"], 30, 3600)
    try:
        with db.get_conn() as conn, db.transaction(conn, immediate=True):
            booking = db.booking_with_customer(conn, booking_id, for_update=True)
            if not booking:
                raise HTTPException(404, "לא נמצא")
            if booking["status"] not in {"pending", "approved"}:
                raise HTTPException(400, "אי אפשר לשנות את מועד התור הזה")
            if booking["booking_date"] == data.date and booking["booking_time"] == data.time:
                return {
                    "ok": True,
                    "already_applied": True,
                    "calendar_synced": booking["calendar_sync_status"] == "synced",
                    "warning": None,
                }
            starts, ends = availability.to_unix(
                data.date, data.time, int(booking["duration_minutes"])
            )
            if starts <= db.now():
                raise HTTPException(400, "יש לבחור מועד עתידי")
            if not availability.within_working_hours(
                conn, data.date, data.time, int(booking["duration_minutes"])
            ):
                raise HTTPException(409, "המועד אינו בתוך שעות הפעילות")
            if db.booking_overlap(conn, starts, ends, exclude_booking_id=booking_id):
                raise HTTPException(409, "המועד כבר תפוס")
            if db.block_overlap(conn, starts, ends):
                raise HTTPException(409, "המועד חסום")
            connected = google_calendar.is_connected(conn)
            sync_status = (
                "pending" if booking["status"] == "approved" and connected else "not_connected"
            )
            conn.execute(
                "UPDATE bookings SET booking_date=?,booking_time=?,starts_at=?,ends_at=?,"
                "calendar_sync_status=?,calendar_sync_error=NULL,updated_at=? WHERE id=?",
                (
                    data.date,
                    data.time,
                    starts,
                    ends,
                    sync_status,
                    db.now(),
                    booking_id,
                ),
            )
            booking.update(
                {
                    "booking_date": data.date,
                    "booking_time": data.time,
                    "starts_at": starts,
                    "ends_at": ends,
                }
            )
            notifications.queue_booking_rescheduled(conn, booking)
    except ValueError:
        raise HTTPException(400, "המועד אינו תקין") from None
    except Exception as exc:
        if is_booking_overlap_error(exc):
            raise HTTPException(409, "המועד כבר תפוס") from None
        raise

    sync_result = {"synced": False, "warning": None}
    if booking["status"] == "approved":
        sync_result = calendar_sync.sync_booking(booking_id)
    try:
        notifications.process_due_jobs(limit=6, booking_id=booking_id)
    except Exception:
        logger.exception("Reschedule email processing failed booking_id=%s", booking_id)
    return {
        "ok": True,
        "already_applied": False,
        "calendar_synced": sync_result["synced"],
        "warning": sync_result["warning"],
    }


@app.post("/api/owner/bookings/{booking_id}/request-arrival")
def request_arrival(booking_id: int):
    already_applied = False
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        booking = db.booking_with_customer(conn, booking_id, for_update=True)
        if not booking or booking["status"] != "approved":
            raise HTTPException(404, "לא נמצא תור מאושר")
        if booking["arrival_status"] == "requested":
            already_applied = True
        else:
            conn.execute(
                "UPDATE bookings SET arrival_status='requested',updated_at=? WHERE id=?",
                (db.now(), booking_id),
            )
            notifications.queue_arrival_request(conn, booking_id)
    if not already_applied:
        try:
            notifications.process_due_jobs(limit=2, booking_id=booking_id)
        except Exception:
            logger.exception("Arrival email processing failed booking_id=%s", booking_id)
    return {"ok": True, "already_applied": already_applied}


@app.post("/api/owner/bookings/{booking_id}/no-show")
def no_show(booking_id: int):
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        booking = db.rowdict(
            conn.execute(
                "SELECT customer_id FROM bookings WHERE id=? AND status='approved'", (booking_id,)
            ).fetchone()
        )
        if not booking:
            raise HTTPException(404, "לא נמצא")
        changed = conn.execute(
            "UPDATE bookings SET arrival_status='no_show',updated_at=? "
            "WHERE id=? AND COALESCE(arrival_status,'')<>'no_show'",
            (db.now(), booking_id),
        )
        if changed.rowcount:
            conn.execute(
                "UPDATE customers SET no_show_count=no_show_count+1 WHERE id=?",
                (booking["customer_id"],),
            )
    return {"ok": True}


@app.get("/api/owner/services")
def services_get():
    with db.get_conn() as conn:
        return {"services": db.all_services(conn)}


@app.post("/api/owner/services")
def services_add(item: ServiceIn):
    with db.get_conn() as conn:
        service_id = db.insert_id(
            conn,
            "INSERT INTO services "
            "(name,category,price,duration_minutes,is_active,display_order) "
            "VALUES (?,?,?,?,?,?)",
            (
                clean_text(item.name, 100),
                clean_text(item.category, 80),
                item.price,
                item.duration_minutes,
                bool_value(item.is_active),
                item.display_order,
            ),
        )
        return {"id": service_id}


@app.put("/api/owner/services/{service_id}")
def services_put(service_id: int, item: ServiceIn):
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE services SET name=?,category=?,price=?,duration_minutes=?,is_active=?,display_order=? WHERE id=?",
            (
                clean_text(item.name, 100),
                clean_text(item.category, 80),
                item.price,
                item.duration_minutes,
                bool_value(item.is_active),
                item.display_order,
                service_id,
            ),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "לא נמצא")
    return {"ok": True}


@app.delete("/api/owner/services/{service_id}")
def services_delete(service_id: int):
    with db.get_conn() as conn:
        cursor = conn.execute("DELETE FROM services WHERE id=?", (service_id,))
        if cursor.rowcount == 0:
            raise HTTPException(404, "לא נמצא")
    return {"ok": True}


@app.get("/api/owner/hours")
def hours_get():
    with db.get_conn() as conn:
        return {"hours": db.working_hours(conn)}


@app.put("/api/owner/hours")
def hours_put(items: list[HoursIn]):
    if len(items) != 7 or {item.day_of_week for item in items} != set(range(7)):
        raise HTTPException(400, "יש לשלוח 7 ימים")
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        for item in items:
            if not bool_value(item.is_closed) and (
                not item.open_time
                or not item.close_time
                or availability.parse_minutes(item.open_time)
                >= availability.parse_minutes(item.close_time)
            ):
                raise HTTPException(400, "שעות פתיחה לא תקינות")
            conn.execute(
                "UPDATE working_hours SET is_closed=?,open_time=?,close_time=?,slot_interval_minutes=? WHERE day_of_week=?",
                (
                    bool_value(item.is_closed),
                    item.open_time,
                    item.close_time,
                    item.slot_interval_minutes,
                    item.day_of_week,
                ),
            )
    return {"ok": True}


@app.get("/api/owner/overrides")
def overrides_get(date_from: str = "1970-01-01", date_to: str = "2100-12-31"):
    validate_date_or_400(date_from)
    validate_date_or_400(date_to)
    if date_to < date_from:
        raise HTTPException(400, "טווח תאריכים לא תקין")
    with db.get_conn() as conn:
        return {"overrides": db.overrides_between(conn, date_from, date_to)}


@app.post("/api/owner/overrides")
def overrides_post(item: OverrideIn):
    is_closed = bool_value(item.is_closed)
    if not is_closed:
        if not item.open_time or not item.close_time:
            raise HTTPException(400, "נדרשות שעות פתיחה וסגירה")
        if availability.parse_minutes(item.open_time) >= availability.parse_minutes(
            item.close_time
        ):
            raise HTTPException(400, "שעות הפתיחה אינן תקינות")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO date_overrides "
            "(override_date,is_closed,open_time,close_time,slot_interval_minutes,internal_note) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT (override_date) DO UPDATE SET "
            "is_closed=excluded.is_closed,open_time=excluded.open_time,"
            "close_time=excluded.close_time,slot_interval_minutes=excluded.slot_interval_minutes,"
            "internal_note=excluded.internal_note",
            (
                item.override_date,
                is_closed,
                item.open_time,
                item.close_time,
                item.slot_interval_minutes,
                clean_text(item.internal_note, 500),
            ),
        )
    return {"ok": True}


@app.delete("/api/owner/overrides/{day}")
def overrides_delete(day: str):
    validate_date_or_400(day)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM date_overrides WHERE override_date=?", (day,))
    return {"ok": True}


@app.get("/api/owner/blocks")
def blocks_get(date_from: str = "1970-01-01", date_to: str = "2100-12-31"):
    validate_date_or_400(date_from)
    validate_date_or_400(date_to)
    if date_to < date_from:
        raise HTTPException(400, "טווח תאריכים לא תקין")
    with db.get_conn() as conn:
        return {"blocks": db.blocks_between(conn, date_from, date_to)}


@app.post("/api/owner/blocks")
def blocks_post(item: BlockIn):
    try:
        starts, ends = availability.to_unix(
            item.blocked_date, item.blocked_time, item.duration_minutes
        )
    except ValueError:
        raise HTTPException(400, "המועד אינו תקין") from None
    with db.get_conn() as conn:
        if db.booking_overlap(conn, starts, ends):
            raise HTTPException(409, "קיים תור במועד הזה")
        block_id = db.insert_id(
            conn,
            "INSERT INTO blocked_slots "
            "(blocked_date,blocked_time,duration_minutes,internal_note,starts_at,ends_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                item.blocked_date,
                item.blocked_time,
                item.duration_minutes,
                clean_text(item.internal_note, 500),
                starts,
                ends,
            ),
        )
        return {"id": block_id}


@app.delete("/api/owner/blocks/{block_id}")
def blocks_delete(block_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM blocked_slots WHERE id=?", (block_id,))
    return {"ok": True}


@app.get("/api/owner/customers")
def customers_get():
    with db.get_conn() as conn:
        return {
            "customers": [
                dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY created_at DESC")
            ]
        }


@app.put("/api/owner/customers/{customer_id}")
def customers_put(customer_id: int, item: CustomerIn):
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE customers SET internal_note=?,is_blocked=? WHERE id=?",
            (clean_text(item.internal_note, 500), bool_value(item.is_blocked), customer_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "לא נמצא")
    return {"ok": True}


@app.get("/api/owner/settings")
def settings_get():
    with db.get_conn() as conn:
        return db.settings(conn)


@app.put("/api/owner/settings")
def settings_put(item: SettingsIn):
    fields = [
        "name",
        "description",
        "address",
        "phone",
        "social_url",
        "waze_url",
        "cover_image",
        "profile_image",
        "preparation_message",
        "min_lead_minutes",
        "max_days_ahead",
    ]
    values = [
        getattr(item, field)
        if field in {"min_lead_minutes", "max_days_ahead"}
        else clean_text(getattr(item, field), 500)
        for field in fields
    ]
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE settings SET name=?,description=?,address=?,phone=?,social_url=?,"
            "waze_url=?,cover_image=?,profile_image=?,preparation_message=?,"
            "min_lead_minutes=?,max_days_ahead=? WHERE id=1",
            values,
        )
    return {"ok": True}


@app.post("/api/owner/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    session = auth.require_owner(request)
    enforce_request_limit("owner-upload", session["token_hash"], 20, 3600)
    data = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(400, "קובץ גדול מדי")
    try:
        url = await storage.save_public_image(data)
    except storage.ImageValidationError:
        raise HTTPException(400, "אפשר להעלות רק תמונת JPEG, PNG או WebP תקינה") from None
    except storage.StorageUnavailableError:
        raise HTTPException(503, "אחסון התמונות אינו מוגדר") from None
    except Exception as exc:
        logger.exception("Owner image upload failed for owner=%s", session["token_hash"])
        raise HTTPException(502, "העלאת התמונה נכשלה. נסו שוב.") from None
    return {"url": url}


@app.post("/api/owner/upload-smoke")
async def upload_smoke(request: Request):
    """Temporary secure smoke endpoint: owner-only, guarded by ENABLE_BLOB_SMOKE_TEST env var.

    This uploads a few bytes to the Vercel Blob store using the SDK defaults
    (no explicit token). Only enabled when ENABLE_BLOB_SMOKE_TEST is truthy.
    """
    if not config.env("ENABLE_BLOB_SMOKE_TEST"):
        raise HTTPException(404)
    session = auth.require_owner(request)
    # tiny 1x1 PNG
    data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\xda\x63\x00\x01\x00\x00\x05\x00\x01\x0d\x0a\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    try:
        url = await storage.save_public_image(data)
    except Exception:
        logger.exception("Owner smoke image upload failed for owner=%s", session["token_hash"])
        raise HTTPException(502, "שגיאת בדיקת העלאה")
    return {"url": url}


@app.get("/uploads/{name}")
def uploaded(name: str):
    if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|webp)", name):
        raise HTTPException(404)
    target = config.UPLOAD_DIR / name
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/cron/reminders")
def run_reminders(request: Request):
    expected = f"Bearer {config.CRON_SECRET}" if config.CRON_SECRET else ""
    supplied = request.headers.get("authorization", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "אין הרשאה")
    email_result = notifications.process_due_jobs(limit=50)
    calendar_result = calendar_sync.sync_pending(limit=25)
    return {"ok": True, "emails": email_result, "calendar": calendar_result}


app.mount("/", StaticFiles(directory=config.BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
