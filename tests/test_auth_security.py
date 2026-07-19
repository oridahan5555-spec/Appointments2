import auth
import config
import db
from tests.helpers import booking_payload, csrf_headers


def test_application_starts_and_sets_security_headers(client):
    response = client.get("/api/business")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "access-control-allow-origin" not in response.headers
    assert client.get("/api/session").json() == {"authenticated": False}


def test_owner_routes_reject_anonymous_and_customer_sessions(client, session_factory):
    assert client.get("/api/owner/settings").status_code == 403

    customer = session_factory()
    response = client.put(
        "/api/owner/settings",
        headers=csrf_headers(customer),
        json={
            "name": "Business",
            "min_lead_minutes": 0,
            "max_days_ahead": 30,
        },
    )

    assert response.status_code == 403


def test_customer_state_changes_require_valid_csrf(client, session_factory):
    session = session_factory()

    assert client.post("/api/bookings", json=booking_payload()).status_code == 403
    assert (
        client.post(
            "/api/bookings",
            headers={"X-CSRF-Token": "wrong"},
            json=booking_payload(),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/bookings",
            headers=csrf_headers(session),
            json=booking_payload(),
        ).status_code
        == 200
    )


def test_customer_otp_is_valid_once_and_creates_session(client, sent_emails):
    email = "new-customer@example.com"
    requested = client.post("/api/auth/request-code", json={"email": email})
    code = sent_emails[-1]["code"]

    verified = client.post("/api/auth/verify", json={"email": email, "code": code})
    replay = client.post("/api/auth/verify", json={"email": email, "code": code})

    assert requested.status_code == 200
    assert len(code) == 6 and code.isdigit()
    assert verified.status_code == 200
    assert verified.json()["role"] == "customer"
    assert verified.json()["csrf_token"]
    assert replay.status_code == 400


def test_otp_expiration_is_enforced(client, sent_emails):
    email = "expired@example.com"
    client.post("/api/auth/request-code", json={"email": email})
    code = sent_emails[-1]["code"]
    with db.get_conn() as conn:
        conn.execute("UPDATE otp_codes SET expires_at=? WHERE email=?", (db.now() - 1, email))

    response = client.post("/api/auth/verify", json={"email": email, "code": code})

    assert response.status_code == 400
    with db.get_conn() as conn:
        assert conn.execute("SELECT 1 FROM otp_codes WHERE email=?", (email,)).fetchone() is None


def test_otp_guessing_is_limited_and_code_is_removed(client, sent_emails):
    email = "guessing@example.com"
    client.post("/api/auth/request-code", json={"email": email})

    for _ in range(10):
        assert (
            client.post("/api/auth/verify", json={"email": email, "code": "000000"}).status_code
            == 400
        )
    limited = client.post("/api/auth/verify", json={"email": email, "code": "000000"})

    assert limited.status_code == 429
    with db.get_conn() as conn:
        assert conn.execute("SELECT 1 FROM otp_codes WHERE email=?", (email,)).fetchone() is None


def test_otp_request_rate_limit_prevents_email_bombing(client, sent_emails):
    email = "limited@example.com"

    for _ in range(3):
        assert client.post("/api/auth/request-code", json={"email": email}).status_code == 200
    limited = client.post("/api/auth/request-code", json={"email": email})

    assert limited.status_code == 429
    assert len(sent_emails) == 3


def test_owner_otp_only_goes_to_configured_owner_and_has_separate_purpose(client, sent_emails):
    wrong = client.post("/api/auth/request-owner-code", json={"email": "someone@example.com"})
    customer_attempt = client.post("/api/auth/request-code", json={"email": config.OWNER_EMAIL})
    assert wrong.status_code == 200
    assert customer_attempt.status_code == 200
    assert sent_emails == []

    client.post("/api/auth/request-owner-code", json={"email": config.OWNER_EMAIL})
    code = sent_emails[-1]["code"]

    assert (
        client.post(
            "/api/auth/verify", json={"email": config.OWNER_EMAIL, "code": code}
        ).status_code
        == 400
    )
    owner = client.post("/api/auth/verify-owner", json={"email": config.OWNER_EMAIL, "code": code})
    assert owner.status_code == 200
    assert owner.json()["role"] == "owner"


def test_session_is_rotated_after_authentication(client, session_factory, sent_emails):
    previous = session_factory(email="rotate@example.com")
    client.post("/api/auth/request-code", json={"email": previous["email"]})
    code = sent_emails[-1]["code"]

    response = client.post("/api/auth/verify", json={"email": previous["email"], "code": code})

    assert response.status_code == 200
    assert (
        client.cookies.get(auth.COOKIE, domain="testserver.local", path="/")
        != previous["raw_token"]
    )
    with db.get_conn() as conn:
        old = conn.execute(
            "SELECT 1 FROM sessions WHERE token_hash=?", (previous["token_hash"],)
        ).fetchone()
    assert old is None


def test_owner_session_persists_for_ninety_days(client, sent_emails):
    client.post("/api/auth/request-owner-code", json={"email": config.OWNER_EMAIL})
    code = sent_emails[-1]["code"]
    before = db.now()

    response = client.post("/api/auth/verify-owner", json={"email": config.OWNER_EMAIL, "code": code})

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert f"max-age={auth.OWNER_SESSION_LIFETIME_SECONDS}" in cookie
    with db.get_conn() as conn:
        row = conn.execute("SELECT expires_at FROM sessions WHERE role='owner'").fetchone()
    assert row["expires_at"] >= before + auth.OWNER_SESSION_LIFETIME_SECONDS - 2


def test_expired_session_and_logout_invalidation(client, session_factory):
    session_factory(expires_at=db.now() - 1)
    assert client.get("/api/me").status_code == 401

    active = session_factory(email="logout@example.com")
    logged_out = client.post("/api/auth/logout", headers=csrf_headers(active))

    assert logged_out.status_code == 200
    assert client.get("/api/me").status_code == 401
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE token_hash=?", (active["token_hash"],)
        ).fetchone()
    assert row is None


def test_production_cookie_flags(client, sent_emails, monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    email = "cookie@example.com"
    client.post("/api/auth/request-code", json={"email": email})
    code = sent_emails[-1]["code"]

    response = client.post("/api/auth/verify", json={"email": email, "code": code})
    cookie = response.headers["set-cookie"].lower()

    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_invalid_email_is_rejected_without_sending(client, sent_emails):
    response = client.post("/api/auth/request-code", json={"email": "not-an-email"})

    assert response.status_code == 400
    assert sent_emails == []
