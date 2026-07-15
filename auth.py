import hashlib
import hmac
import secrets
import time

from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException, Request, Response

import config
import db

COOKIE = "booking_session"


def normalize_email(value: str) -> str:
    try:
        result = validate_email(value.strip(), check_deliverability=False)
    except (AttributeError, EmailNotValidError):
        raise HTTPException(400, "כתובת המייל לא תקינה") from None
    return result.normalized.lower()


def otp_hash(code: str) -> str:
    secret = config.require_secret("OTP_SECRET")
    return hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, response: Response, email: str, role: str, customer_id):
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + 90 * 24 * 3600
    conn.execute(
        "INSERT INTO sessions (token_hash,customer_id,email,role,expires_at,created_at) VALUES (?,?,?,?,?,?)",
        (token_hash(token), customer_id, email, role, expires_at, db.now()),
    )
    response.set_cookie(COOKIE, token, httponly=True, secure=config.COOKIE_SECURE, samesite="lax", max_age=90 * 24 * 3600)


def session_from_request(conn, request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash(token), db.now())).fetchone()
    return dict(row) if row else None


def require_customer(request: Request):
    with db.get_conn() as conn:
        session = session_from_request(conn, request)
        if not session or session["role"] not in ("customer", "owner"):
            raise HTTPException(401, "נדרש אימות")
        return session


def require_owner(request: Request):
    with db.get_conn() as conn:
        session = session_from_request(conn, request)
        if not session or session["role"] != "owner":
            raise HTTPException(403, "אין הרשאה")
        return session
