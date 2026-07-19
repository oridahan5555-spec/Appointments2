import hashlib
import hmac
import ipaddress
import secrets
import time

from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException, Request, Response

import config
import db

COOKIE = "booking_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CUSTOMER_SESSION_LIFETIME_SECONDS = 30 * 24 * 3600
OWNER_SESSION_LIFETIME_SECONDS = 90 * 24 * 3600


def normalize_email(value: str) -> str:
    try:
        result = validate_email(value.strip(), check_deliverability=False)
    except (AttributeError, EmailNotValidError):
        raise HTTPException(400, "כתובת המייל לא תקינה") from None
    return result.normalized.lower()


def otp_hash(email: str, purpose: str, code: str) -> str:
    secret = config.require_secret("OTP_SECRET")
    message = f"{email}\0{purpose}\0{code}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def rate_identity(scope: str, value: str) -> str:
    secret = config.require_secret("SESSION_SECRET")
    message = f"{scope}\0{value}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def client_ip(request: Request) -> str:
    candidates: list[str] = []
    if config.TRUST_PROXY_HEADERS:
        if request.headers.get("x-real-ip"):
            candidates.append(request.headers["x-real-ip"])
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            candidates.append(forwarded.split(",", 1)[0])
    if request.client:
        candidates.append(request.client.host)
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate.strip()))
        except ValueError:
            continue
    return "unknown"


def create_session(
    conn: db.Connection,
    response: Response,
    email: str,
    role: str,
    customer_id: int | None,
    previous_token: str | None = None,
) -> str:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    lifetime = (
        OWNER_SESSION_LIFETIME_SECONDS
        if role == "owner"
        else CUSTOMER_SESSION_LIFETIME_SECONDS
    )
    timestamp = int(time.time())
    if previous_token:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash=?",
            (token_hash(previous_token),),
        )
    conn.execute("DELETE FROM sessions WHERE expires_at<=?", (timestamp,))
    conn.execute(
        "INSERT INTO sessions "
        "(token_hash,csrf_token,customer_id,email,role,expires_at,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            token_hash(token),
            csrf_token,
            customer_id,
            email,
            role,
            timestamp + lifetime,
            timestamp,
        ),
    )
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        max_age=lifetime,
        path="/",
    )
    return csrf_token


def session_from_request(conn: db.Connection, request: Request):
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 256:
        return None
    hashed = token_hash(token)
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?",
        (hashed, db.now()),
    ).fetchone()
    if not row:
        return None
    session = dict(row)
    if not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE sessions SET csrf_token=? WHERE token_hash=?",
            (session["csrf_token"], hashed),
        )
    session["token_hash"] = hashed
    return session


def require_customer(request: Request):
    cached = getattr(request.state, "session", None)
    if cached and cached["role"] in ("customer", "owner"):
        return cached
    with db.get_conn() as conn:
        session = session_from_request(conn, request)
    if not session or session["role"] not in ("customer", "owner"):
        raise HTTPException(401, "נדרש אימות")
    request.state.session = session
    return session


def require_owner(request: Request):
    cached = getattr(request.state, "session", None)
    if cached and cached["role"] == "owner":
        return cached
    with db.get_conn() as conn:
        session = session_from_request(conn, request)
    if not session or session["role"] != "owner":
        raise HTTPException(403, "אין הרשאה")
    request.state.session = session
    return session


def require_csrf(request: Request, session: dict | None = None) -> None:
    if request.method.upper() in SAFE_METHODS:
        return
    session = session or require_customer(request)
    supplied = request.headers.get("x-csrf-token", "")
    expected = session.get("csrf_token") or ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, "בקשת האבטחה אינה תקינה. רעננו את הדף ונסו שוב.")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE,
        path="/",
        secure=config.COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
