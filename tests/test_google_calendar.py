import io
import json
import urllib.error
from urllib.parse import parse_qs, urlparse

import availability
import calendar_sync
import config
import db
import google_calendar
import secret_crypto
from tests.helpers import booking_payload, csrf_headers, future_open_day


def enable_google(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ENABLED", True)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-id.example.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "primary")


def approved_booking() -> dict:
    day = future_open_day()
    starts, ends = availability.to_unix(day, "10:00", 30)
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        customer_id = db.upsert_customer(conn, "calendar-customer@example.com", "Dana")
        services = db.active_services_by_ids(conn, [1])
        booking_id = db.insert_booking(
            conn,
            customer_id,
            [1],
            services,
            day,
            "10:00",
            "Calendar notes",
            starts,
            ends,
        )
        conn.execute("UPDATE bookings SET status='approved' WHERE id=?", (booking_id,))
        return db.booking_with_customer(conn, booking_id)


def test_refresh_token_is_encrypted_at_rest_and_decrypts(monkeypatch):
    enable_google(monkeypatch)
    plaintext = "refresh-token-plain-value"

    with db.get_conn() as conn:
        google_calendar.store_refresh_token(conn, plaintext)
        stored = db.get_secret(conn, "google_refresh_token")
        recovered = google_calendar.refresh_token(conn)

    assert stored != plaintext
    assert secret_crypto.is_encrypted(stored)
    assert recovered == plaintext
    assert plaintext not in stored


def test_google_oauth_state_is_bound_to_session_single_use_and_exact_redirect(
    client, session_factory, monkeypatch
):
    enable_google(monkeypatch)
    owner = session_factory(role="owner")
    monkeypatch.setattr(
        google_calendar,
        "exchange_code_for_refresh_token",
        lambda code, redirect_uri: "new-refresh-token",
    )

    started = client.post("/api/owner/google/connect", headers=csrf_headers(owner))
    authorization_url = started.json()["authorization_url"]
    params = parse_qs(urlparse(authorization_url).query)
    state = params["state"][0]
    redirect_uri = params["redirect_uri"][0]

    callback = client.get(
        "/api/owner/google/callback",
        params={"state": state, "code": "authorization-code"},
        follow_redirects=False,
    )
    replay = client.get(
        "/api/owner/google/callback",
        params={"state": state, "code": "authorization-code"},
        follow_redirects=False,
    )

    assert started.status_code == 200
    assert params["scope"] == [google_calendar.SCOPE]
    assert redirect_uri == config.GOOGLE_REDIRECT_URI
    assert callback.status_code == 302
    assert callback.headers["location"] == "/owner.html?google=connected"
    assert replay.status_code == 400
    with db.get_conn() as conn:
        stored = db.get_secret(conn, "google_refresh_token")
    assert secret_crypto.is_encrypted(stored)
    assert "new-refresh-token" not in stored


def test_google_oauth_state_rejects_other_session_and_expired_state(
    client, session_factory, monkeypatch
):
    enable_google(monkeypatch)
    first = session_factory(role="owner")
    started = client.post("/api/owner/google/connect", headers=csrf_headers(first))
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]

    second = session_factory(role="owner")
    wrong_session = client.get(
        "/api/owner/google/callback",
        params={"state": state, "code": "code"},
        follow_redirects=False,
    )
    assert wrong_session.status_code == 400

    restarted = client.post("/api/owner/google/connect", headers=csrf_headers(second))
    expired_state = parse_qs(urlparse(restarted.json()["authorization_url"]).query)["state"][0]
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE oauth_states SET expires_at=? WHERE state=?",
            (db.now() - 1, expired_state),
        )
    expired = client.get(
        "/api/owner/google/callback",
        params={"state": expired_state, "code": "code"},
        follow_redirects=False,
    )
    assert expired.status_code == 400


def test_create_event_has_deterministic_id_timezone_and_15_minute_reminder(
    monkeypatch,
):
    enable_google(monkeypatch)
    booking = approved_booking()
    captured = []
    monkeypatch.setattr(google_calendar, "access_token", lambda _conn: "access-token")

    def fake_request(request):
        captured.append(request)
        return {"id": google_calendar.event_id_for_booking(booking["id"])}

    monkeypatch.setattr(google_calendar, "_request_json", fake_request)
    with db.get_conn() as conn:
        event_id = google_calendar.create_event(conn, booking, db.settings(conn))

    request = captured[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert request.method == "POST"
    assert event_id == google_calendar.event_id_for_booking(booking["id"])
    assert payload["id"] == event_id
    assert payload["start"]["timeZone"] == "Asia/Jerusalem"
    assert payload["end"]["timeZone"] == "Asia/Jerusalem"
    assert payload["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 15}],
    }
    assert "Dana" in payload["summary"]
    assert request.get_header("Authorization") == "Bearer access-token"


def test_duplicate_create_retries_as_idempotent_put(monkeypatch):
    enable_google(monkeypatch)
    booking = approved_booking()
    methods = []
    monkeypatch.setattr(google_calendar, "access_token", lambda _conn: "access-token")

    def fake_request(request):
        methods.append(request.method)
        if request.method == "POST":
            raise google_calendar.GoogleCalendarError("duplicate", 409)
        return {"id": google_calendar.event_id_for_booking(booking["id"])}

    monkeypatch.setattr(google_calendar, "_request_json", fake_request)
    with db.get_conn() as conn:
        event_id = google_calendar.create_event(conn, booking, db.settings(conn))

    assert event_id == google_calendar.event_id_for_booking(booking["id"])
    assert methods == ["POST", "PUT"]


def test_update_recreates_event_after_google_404(monkeypatch):
    enable_google(monkeypatch)
    booking = approved_booking()
    methods = []
    monkeypatch.setattr(google_calendar, "access_token", lambda _conn: "access-token")

    def fake_request(request):
        methods.append(request.method)
        if request.method == "PUT":
            raise google_calendar.GoogleCalendarError("missing", 404)
        return {"id": google_calendar.event_id_for_booking(booking["id"])}

    monkeypatch.setattr(google_calendar, "_request_json", fake_request)
    with db.get_conn() as conn:
        event_id = google_calendar.update_event(
            conn, "manually-deleted-event", booking, db.settings(conn)
        )

    assert event_id == google_calendar.event_id_for_booking(booking["id"])
    assert methods == ["PUT", "POST"]


def test_delete_treats_missing_event_as_success(monkeypatch):
    enable_google(monkeypatch)
    monkeypatch.setattr(google_calendar, "is_connected", lambda _conn: True)
    monkeypatch.setattr(google_calendar, "access_token", lambda _conn: "access-token")

    def missing(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://www.googleapis.com/calendar/v3/test",
            404,
            "Not found",
            hdrs=None,
            fp=io.BytesIO(b"not found"),
        )

    monkeypatch.setattr(google_calendar.urllib.request, "urlopen", missing)
    with db.get_conn() as conn:
        google_calendar.delete_event(conn, "event-id")


def test_revoked_refresh_token_marks_calendar_disconnected(monkeypatch):
    enable_google(monkeypatch)
    with db.get_conn() as conn:
        google_calendar.store_refresh_token(conn, "revoked-refresh-token")

    def revoked(_request):
        raise google_calendar.GoogleCalendarError("invalid grant", 400)

    monkeypatch.setattr(google_calendar, "_request_json", revoked)
    with db.get_conn() as conn:
        try:
            google_calendar.access_token(conn)
        except google_calendar.GoogleCalendarError as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("revoked token was accepted")
        assert db.get_secret(conn, "google_refresh_token") is None
        assert db.get_secret(conn, "google_disconnected") == "1"
        assert google_calendar.refresh_token(conn) is None


def test_calendar_api_failure_does_not_roll_back_approval(client, session_factory, monkeypatch):
    enable_google(monkeypatch)
    customer = session_factory(email="approval-calendar@example.com")
    booking_id = client.post(
        "/api/bookings",
        headers=csrf_headers(customer),
        json=booking_payload(),
    ).json()["id"]
    with db.get_conn() as conn:
        google_calendar.store_refresh_token(conn, "valid-refresh-token")

    def fail(*_args, **_kwargs):
        raise google_calendar.GoogleCalendarError("provider unavailable", 503)

    monkeypatch.setattr(google_calendar, "update_event", fail)
    owner = session_factory(role="owner")
    response = client.post(
        f"/api/owner/bookings/{booking_id}/status",
        headers=csrf_headers(owner),
        json={"status": "approved"},
    )

    assert response.status_code == 200
    assert response.json()["calendar_synced"] is False
    assert response.json()["warning"]
    with db.get_conn() as conn:
        booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    assert booking["status"] == "approved"
    assert booking["calendar_sync_status"] == "failed"
    assert booking["calendar_sync_error"] == "google-http-503"


def test_failed_calendar_delete_keeps_event_id_for_cron_retry(monkeypatch):
    enable_google(monkeypatch)
    booking = approved_booking()
    with db.get_conn() as conn:
        google_calendar.store_refresh_token(conn, "valid-refresh-token")
        conn.execute(
            "UPDATE bookings SET google_calendar_event_id=?,calendar_sync_status='synced' "
            "WHERE id=?",
            ("existing-event", booking["id"]),
        )
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking["id"],))

    monkeypatch.setattr(
        google_calendar,
        "delete_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            google_calendar.GoogleCalendarError("temporary", 503)
        ),
    )
    result = calendar_sync.sync_booking(booking["id"])

    assert result["synced"] is False
    with db.get_conn() as conn:
        saved = conn.execute("SELECT * FROM bookings WHERE id=?", (booking["id"],)).fetchone()
    assert saved["google_calendar_event_id"] == "existing-event"
    assert saved["calendar_sync_status"] == "failed"
