import db
import google_calendar


def sync_booking(booking_id: int) -> dict:
    with db.get_conn() as conn:
        booking = db.booking_with_customer(conn, booking_id)
        if not booking:
            return {"synced": False, "warning": "התור לא נמצא"}
        settings = db.settings(conn)
        connected = google_calendar.is_connected(conn)

    if booking["status"] == "approved" and not connected:
        _record(booking_id, "not_connected", None, booking.get("google_calendar_event_id"))
        return {"synced": False, "warning": "Google Calendar אינו מחובר"}
    if (
        booking["status"] == "cancelled"
        and booking.get("google_calendar_event_id")
        and not connected
    ):
        _record(booking_id, "failed", "calendar-not-connected", booking["google_calendar_event_id"])
        return {"synced": False, "warning": "התור בוטל, אך נדרש לחבר את Google כדי למחוק את האירוע"}

    try:
        with db.get_conn() as conn:
            if booking["status"] == "approved":
                event_id = google_calendar.update_event(
                    conn,
                    booking.get("google_calendar_event_id"),
                    booking,
                    settings,
                )
                _record(booking_id, "synced", None, event_id)
                return {"synced": True, "warning": None}
            if booking["status"] in {"cancelled", "rejected"}:
                if booking.get("google_calendar_event_id"):
                    google_calendar.delete_event(conn, booking["google_calendar_event_id"])
                _record(booking_id, "synced", None, None)
                return {"synced": True, "warning": None}
    except google_calendar.GoogleCalendarError as exc:
        error = f"google-http-{exc.status_code}" if exc.status_code else "google-request-failed"
        _record(booking_id, "failed", error, booking.get("google_calendar_event_id"))
        return {
            "synced": False,
            "warning": "התור נשמר, אבל הסנכרון עם Google Calendar נכשל וינוסה שוב.",
        }

    return {"synced": False, "warning": None}


def _record(
    booking_id: int,
    status: str,
    error: str | None,
    event_id: str | None,
) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE bookings SET google_calendar_event_id=?,calendar_sync_status=?,"
            "calendar_sync_error=?,calendar_synced_at=?,updated_at=? WHERE id=?",
            (
                event_id,
                status,
                error,
                db.now() if status == "synced" else None,
                db.now(),
                booking_id,
            ),
        )


def sync_pending(limit: int = 20) -> dict:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM bookings WHERE "
            "(status='approved' AND calendar_sync_status IN ('pending','failed')) "
            "OR (status='cancelled' AND google_calendar_event_id IS NOT NULL) "
            "ORDER BY updated_at LIMIT ?",
            (max(1, min(limit, 100)),),
        )
        booking_ids = [int(row["id"]) for row in rows]
    synced = failed = 0
    for booking_id in booking_ids:
        result = sync_booking(booking_id)
        if result["synced"]:
            synced += 1
        elif result["warning"]:
            failed += 1
    return {"processed": len(booking_ids), "synced": synced, "failed": failed}
