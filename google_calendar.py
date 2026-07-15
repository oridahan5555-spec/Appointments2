import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
SCOPE = "https://www.googleapis.com/auth/calendar.events"


class GoogleCalendarError(RuntimeError):
    pass


def oauth_ready() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def is_connected(conn) -> bool:
    return bool(oauth_ready() and refresh_token(conn))


def refresh_token(conn) -> str | None:
    import db

    return db.get_secret(conn, "google_refresh_token") or config.GOOGLE_REFRESH_TOKEN or None


def authorization_url(redirect_uri: str, state: str) -> str:
    if not oauth_ready():
        raise GoogleCalendarError("Google OAuth client id and secret are missing")
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_refresh_token(code: str, redirect_uri: str) -> str:
    if not oauth_ready():
        raise GoogleCalendarError("Google OAuth client id and secret are missing")
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
    return token


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
    data = _request_json(request)
    access = data.get("access_token")
    if not access:
        raise GoogleCalendarError("Google did not return an access token")
    return access


def create_event(conn, booking: dict, settings: dict) -> str:
    token = access_token(conn)
    url = EVENTS_URL.format(calendar_id=urllib.parse.quote(config.GOOGLE_CALENDAR_ID, safe=""))
    request = urllib.request.Request(
        url,
        data=json.dumps(_event_payload(booking, settings), ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    data = _request_json(request)
    event_id = data.get("id")
    if not event_id:
        raise GoogleCalendarError("Google did not return an event id")
    return event_id


def update_event(conn, event_id: str, booking: dict, settings: dict) -> str:
    if not event_id:
        return create_event(conn, booking, settings)
    token = access_token(conn)
    url = EVENTS_URL.format(calendar_id=urllib.parse.quote(config.GOOGLE_CALENDAR_ID, safe="")) + "/" + urllib.parse.quote(event_id, safe="")
    request = urllib.request.Request(
        url,
        data=json.dumps(_event_payload(booking, settings), ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="PUT",
    )
    data = _request_json(request)
    return data.get("id") or event_id


def delete_event(conn, event_id: str) -> None:
    if not event_id or not is_connected(conn):
        return
    token = access_token(conn)
    url = EVENTS_URL.format(calendar_id=urllib.parse.quote(config.GOOGLE_CALENDAR_ID, safe="")) + "/" + urllib.parse.quote(event_id, safe="")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 410):
            raise GoogleCalendarError(f"Google Calendar delete failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GoogleCalendarError(f"Google Calendar delete failed: {type(exc).__name__}") from exc


def _event_payload(booking: dict, settings: dict) -> dict:
    tz = ZoneInfo(config.TZ_NAME)
    starts = datetime.fromtimestamp(booking["starts_at"], tz)
    ends = datetime.fromtimestamp(booking["ends_at"], tz)
    services = json.loads(booking.get("services_snapshot") or "[]")
    service_names = ", ".join(service.get("name", "") for service in services if service.get("name"))
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
        "summary": summary,
        "description": "\n".join(description_parts),
        "location": settings.get("address") or "",
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
        body = exc.read().decode("utf-8", errors="replace")
        raise GoogleCalendarError(f"Google Calendar failed: HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise GoogleCalendarError(f"Google Calendar failed: {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise GoogleCalendarError("Google returned invalid JSON") from exc
