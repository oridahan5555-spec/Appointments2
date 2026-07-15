import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import secret_crypto

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
SCOPE = "https://www.googleapis.com/auth/calendar.events"


class GoogleCalendarError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def oauth_ready() -> bool:
    return bool(
        config.GOOGLE_CALENDAR_ENABLED and config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET
    )


def is_connected(conn) -> bool:
    return bool(oauth_ready() and refresh_token(conn))


def refresh_token(conn) -> str | None:
    import db

    if db.get_secret(conn, "google_disconnected") == "1":
        return None
    stored = db.get_secret(conn, "google_refresh_token")
    if stored:
        if not secret_crypto.is_encrypted(stored):
            stored = secret_crypto.encrypt(stored)
            db.set_secret(conn, "google_refresh_token", stored)
        return secret_crypto.decrypt(stored)
    return config.GOOGLE_REFRESH_TOKEN or None


def store_refresh_token(conn, token: str) -> None:
    import db

    db.set_secret(conn, "google_refresh_token", secret_crypto.encrypt(token))
    db.delete_secret(conn, "google_disconnected")


def authorization_url(redirect_uri: str, state: str) -> str:
    if not oauth_ready():
        raise GoogleCalendarError("Google OAuth is not configured")
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_refresh_token(code: str, redirect_uri: str) -> str:
    if not oauth_ready():
        raise GoogleCalendarError("Google OAuth is not configured")
    payload = urllib.parse.urlencode(
        {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    data = _request_json(request)
    token = data.get("refresh_token")
    if not token:
        raise GoogleCalendarError("Google did not return a refresh token")
    return str(token)


def access_token(conn) -> str:
    token = refresh_token(conn)
    if not oauth_ready() or not token:
        raise GoogleCalendarError("Google Calendar is not connected")
    payload = urllib.parse.urlencode(
        {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "refresh_token": token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        data = _request_json(request)
    except GoogleCalendarError as exc:
        if exc.status_code in (400, 401):
            import db

            db.delete_secret(conn, "google_refresh_token")
            db.set_secret(conn, "google_disconnected", "1")
            raise GoogleCalendarError("Google Calendar authorization was revoked", 401) from exc
        raise
    access = data.get("access_token")
    if not access:
        raise GoogleCalendarError("Google did not return an access token")
    return str(access)


def event_id_for_booking(booking_id: int) -> str:
    return f"appt{booking_id:016x}"


def create_event(conn, booking: dict, settings: dict) -> str:
    token = access_token(conn)
    event_id = event_id_for_booking(int(booking["id"]))
    payload = _event_payload(booking, settings)
    payload["id"] = event_id
    url = EVENTS_URL.format(calendar_id=urllib.parse.quote(config.GOOGLE_CALENDAR_ID, safe=""))
    request = _event_request(url, token, payload, "POST")
    try:
        data = _request_json(request)
    except GoogleCalendarError as exc:
        if exc.status_code == 409:
            return _put_event(conn, event_id, booking, settings, token)
        raise
    returned_id = data.get("id")
    if not returned_id:
        raise GoogleCalendarError("Google did not return an event id")
    return str(returned_id)


def _put_event(conn, event_id: str, booking: dict, settings: dict, token: str | None = None) -> str:
    token = token or access_token(conn)
    url = _event_url(event_id)
    data = _request_json(_event_request(url, token, _event_payload(booking, settings), "PUT"))
    return str(data.get("id") or event_id)


def update_event(conn, event_id: str | None, booking: dict, settings: dict) -> str:
    if not event_id:
        return create_event(conn, booking, settings)
    try:
        return _put_event(conn, event_id, booking, settings)
    except GoogleCalendarError as exc:
        if exc.status_code in (404, 410):
            return create_event(conn, booking, settings)
        raise


def delete_event(conn, event_id: str) -> None:
    if not event_id or not is_connected(conn):
        return
    token = access_token(conn)
    request = urllib.request.Request(
        _event_url(event_id),
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 410):
            raise GoogleCalendarError(
                f"Google Calendar delete failed with HTTP {exc.code}", exc.code
            ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GoogleCalendarError("Google Calendar delete request failed") from exc


def revoke_and_disconnect(conn) -> bool:
    import db

    token = refresh_token(conn)
    revoked = False
    if token:
        request = urllib.request.Request(
            REVOKE_URL,
            data=urllib.parse.urlencode({"token": token}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=15).close()
            revoked = True
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            revoked = False
    db.delete_secret(conn, "google_refresh_token")
    db.set_secret(conn, "google_disconnected", "1")
    return revoked


def _event_url(event_id: str) -> str:
    base = EVENTS_URL.format(calendar_id=urllib.parse.quote(config.GOOGLE_CALENDAR_ID, safe=""))
    return f"{base}/{urllib.parse.quote(event_id, safe='')}"


def _event_request(url: str, token: str, payload: dict, method: str):
    return urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method=method,
    )


def _event_payload(booking: dict, settings: dict) -> dict:
    tz = ZoneInfo(config.TZ_NAME)
    starts = datetime.fromtimestamp(booking["starts_at"], tz)
    ends = datetime.fromtimestamp(booking["ends_at"], tz)
    try:
        services = json.loads(booking.get("services_snapshot") or "[]")
    except json.JSONDecodeError:
        services = []
    service_names = ", ".join(
        str(service.get("name", "")) for service in services if service.get("name")
    )
    customer_name = booking.get("customer_name") or "לקוחה"
    summary = f"תור עם {customer_name}"
    if service_names:
        summary += f" - {service_names}"
    description_parts = [
        f"לקוחה: {customer_name}",
        f"מייל: {booking.get('customer_email') or ''}",
        f"שירותים: {service_names or 'תור'}",
    ]
    if booking.get("notes"):
        description_parts.append(f"הערות: {booking['notes']}")
    return {
        "summary": summary[:1024],
        "description": "\n".join(description_parts)[:8192],
        "location": str(settings.get("address") or "")[:1024],
        "start": {"dateTime": starts.isoformat(), "timeZone": config.TZ_NAME},
        "end": {"dateTime": ends.isoformat(), "timeZone": config.TZ_NAME},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 15}],
        },
    }


def _request_json(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        exc.read()
        raise GoogleCalendarError(
            f"Google Calendar request failed with HTTP {exc.code}", exc.code
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GoogleCalendarError("Google Calendar request failed") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GoogleCalendarError("Google returned an invalid response") from exc
