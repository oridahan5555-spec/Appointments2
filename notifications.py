import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import config
import db
import mailer

REMINDER_KINDS = {"reminder_24h", "reminder_3h"}


def _queue_job(
    conn: db.Connection,
    booking_id: int,
    kind: str,
    recipient_kind: str,
    scheduled_at: int,
    *,
    reset: bool = False,
) -> None:
    existing = conn.execute(
        "SELECT id,status FROM notification_jobs "
        "WHERE booking_id=? AND kind=? AND recipient_kind=?",
        (booking_id, kind, recipient_kind),
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO notification_jobs "
            "(booking_id,kind,recipient_kind,scheduled_at,status,attempts,"
            "next_attempt_at,created_at) VALUES (?,?,?,?,'pending',0,?,?)",
            (booking_id, kind, recipient_kind, scheduled_at, scheduled_at, db.now()),
        )
        return
    if reset:
        conn.execute(
            "UPDATE notification_jobs SET scheduled_at=?,status='pending',attempts=0,"
            "next_attempt_at=?,locked_at=NULL,sent_at=NULL,last_error=NULL WHERE id=?",
            (scheduled_at, scheduled_at, existing["id"]),
        )


def queue_booking_created(conn: db.Connection, booking_id: int) -> None:
    timestamp = db.now()
    _queue_job(conn, booking_id, "booking_received", "customer", timestamp)
    _queue_job(conn, booking_id, "owner_booking_requested", "owner", timestamp)


def queue_booking_approved(conn: db.Connection, booking: dict) -> None:
    timestamp = db.now()
    booking_id = int(booking["id"])
    _queue_job(conn, booking_id, "booking_approved", "customer", timestamp)
    for kind, seconds_before in (("reminder_24h", 24 * 3600), ("reminder_3h", 3 * 3600)):
        scheduled_at = int(booking["starts_at"]) - seconds_before
        if scheduled_at > timestamp + 60:
            for recipient_kind in ("customer", "owner"):
                _queue_job(
                    conn,
                    booking_id,
                    kind,
                    recipient_kind,
                    scheduled_at,
                    reset=True,
                )


def queue_booking_rejected(conn: db.Connection, booking_id: int) -> None:
    _cancel_reminders(conn, booking_id)
    _queue_job(conn, booking_id, "booking_rejected", "customer", db.now())


def queue_booking_cancelled(
    conn: db.Connection,
    booking_id: int,
    *,
    notify_customer: bool,
    notify_owner: bool,
) -> None:
    _cancel_reminders(conn, booking_id)
    if notify_customer:
        _queue_job(conn, booking_id, "booking_cancelled", "customer", db.now())
    if notify_owner:
        _queue_job(conn, booking_id, "owner_booking_cancelled", "owner", db.now())


def queue_booking_rescheduled(conn: db.Connection, booking: dict) -> None:
    _cancel_reminders(conn, int(booking["id"]))
    _queue_job(
        conn,
        int(booking["id"]),
        "booking_rescheduled",
        "customer",
        db.now(),
        reset=True,
    )
    if booking.get("status") == "approved":
        queue_booking_approved(conn, booking)


def queue_arrival_request(conn: db.Connection, booking_id: int) -> None:
    _queue_job(
        conn,
        booking_id,
        "arrival_requested",
        "customer",
        db.now(),
        reset=True,
    )


def _cancel_reminders(conn: db.Connection, booking_id: int) -> None:
    conn.execute(
        "UPDATE notification_jobs SET status='cancelled' "
        "WHERE booking_id=? AND kind IN ('reminder_24h','reminder_3h') "
        "AND status IN ('pending','failed','processing')",
        (booking_id,),
    )


def process_due_jobs(limit: int = 20, booking_id: int | None = None) -> dict:
    claimed = _claim_due_jobs(limit, booking_id)
    sent = failed = cancelled = 0
    for job in claimed:
        outcome = _deliver_job(job)
        if outcome == "sent":
            sent += 1
        elif outcome == "cancelled":
            cancelled += 1
        else:
            failed += 1
    return {"claimed": len(claimed), "sent": sent, "failed": failed, "cancelled": cancelled}


def _claim_due_jobs(limit: int, booking_id: int | None) -> list[dict]:
    timestamp = db.now()
    claimed: list[dict] = []
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        conn.execute(
            "UPDATE notification_jobs SET status='failed',locked_at=NULL,"
            "next_attempt_at=? WHERE status='processing' AND locked_at<?",
            (timestamp, timestamp - 600),
        )
        sql = (
            "SELECT * FROM notification_jobs WHERE status IN ('pending','failed') "
            "AND attempts<5 AND scheduled_at<=? AND next_attempt_at<=?"
        )
        params: list[object] = [timestamp, timestamp]
        if booking_id is not None:
            sql += " AND booking_id=?"
            params.append(booking_id)
        sql += " ORDER BY scheduled_at,id LIMIT ?"
        params.append(max(1, min(limit, 100)))
        rows = [dict(row) for row in conn.execute(sql, params)]
        for row in rows:
            cursor = conn.execute(
                "UPDATE notification_jobs SET status='processing',locked_at=?,attempts=attempts+1 "
                "WHERE id=? AND status IN ('pending','failed')",
                (timestamp, row["id"]),
            )
            if cursor.rowcount:
                row["attempts"] = int(row["attempts"]) + 1
                claimed.append(row)
    return claimed


def _deliver_job(job: dict) -> str:
    with db.get_conn() as conn:
        booking = db.booking_with_customer(conn, int(job["booking_id"]))
        settings = db.settings(conn)
    if not booking or (job["kind"] in REMINDER_KINDS and booking["status"] != "approved"):
        _finish_job(job, "cancelled", "booking-not-active")
        return "cancelled"

    recipient = (
        booking["customer_email"] if job["recipient_kind"] == "customer" else config.OWNER_EMAIL
    )
    title, message = _copy(job["kind"], job["recipient_kind"])
    details = _details(booking, include_customer=job["recipient_kind"] == "owner")
    attach_ics = job["kind"] in {
        "booking_approved",
        "booking_rescheduled",
        "reminder_24h",
        "reminder_3h",
    }
    status = mailer.send_email(
        recipient,
        job["kind"],
        title,
        message,
        details=details,
        ics_content=calendar_file(booking, settings) if attach_ics else None,
    )
    if status.startswith("mailjet:2"):
        _finish_job(job, "sent", None)
        return "sent"
    _finish_job(job, "failed", status[:120])
    return "failed"


def _finish_job(job: dict, status: str, error: str | None) -> None:
    timestamp = db.now()
    attempts = int(job.get("attempts") or 1)
    next_attempt = timestamp + min(6 * 3600, 60 * (2 ** min(attempts, 6)))
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE notification_jobs SET status=?,locked_at=NULL,sent_at=?,"
            "last_error=?,next_attempt_at=? WHERE id=?",
            (
                status,
                timestamp if status == "sent" else None,
                error,
                next_attempt,
                job["id"],
            ),
        )


def _copy(kind: str, recipient_kind: str) -> tuple[str, str]:
    copies = {
        "booking_received": ("בקשת התור התקבלה", "הבקשה הועברה לבעלת העסק וממתינה לאישור."),
        "owner_booking_requested": ("בקשת תור חדשה", "נכנסה בקשת תור חדשה שממתינה לאישור שלך."),
        "booking_approved": ("התור שלך אושר", "התור אושר ונוסף קובץ יומן לצירוף."),
        "booking_rejected": ("בקשת התור לא אושרה", "בקשת התור לא אושרה. אפשר לבחור מועד אחר באתר."),
        "booking_cancelled": ("התור בוטל", "התור בוטל והמועד שוחרר."),
        "owner_booking_cancelled": (
            "לקוחה ביטלה תור",
            "הלקוחה ביטלה את התור והמועד חזר להיות פנוי.",
        ),
        "booking_rescheduled": ("מועד התור השתנה", "מועד התור עודכן. הפרטים החדשים מצורפים למייל."),
        "arrival_requested": (
            "בקשת אישור הגעה",
            "בעלת העסק מבקשת לאשר הגעה לתור דרך אזור התורים באתר.",
        ),
        "reminder_24h": ("תזכורת לתור מחר", "זו תזכורת לתור שיתקיים בעוד כיממה."),
        "reminder_3h": ("תזכורת לתור בקרוב", "זו תזכורת לתור שיתקיים בעוד כשלוש שעות."),
    }
    title, message = copies[kind]
    if recipient_kind == "owner" and kind in REMINDER_KINDS:
        message = "תזכורת: יש לך תור עם לקוחה בקרוב."
    return title, message


def _services(booking: dict) -> str:
    try:
        services = json.loads(booking.get("services_snapshot") or "[]")
    except json.JSONDecodeError:
        return "תור"
    names = [str(item.get("name")) for item in services if item.get("name")]
    return ", ".join(names) or "תור"


def _details(booking: dict, *, include_customer: bool) -> list[tuple[str, str]]:
    details = [
        ("שירות", _services(booking)),
        ("תאריך", str(booking["booking_date"])),
        ("שעה", str(booking["booking_time"])),
    ]
    if include_customer:
        details.insert(0, ("לקוחה", str(booking.get("customer_name") or "")))
    return details


def _ics_escape(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\r", "")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def calendar_file(booking: dict, settings: dict) -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    summary = f"{settings.get('name') or 'תור'} - {_services(booking)}"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "PRODID:-//Appointments//Booking//HE",
        "BEGIN:VEVENT",
        f"UID:booking-{int(booking['id'])}@appointments",
        f"DTSTAMP:{datetime.now(UTC).strftime(fmt)}",
        f"DTSTART:{datetime.fromtimestamp(booking['starts_at'], UTC).strftime(fmt)}",
        f"DTEND:{datetime.fromtimestamp(booking['ends_at'], UTC).strftime(fmt)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape('תור במערכת הזימונים')}",
        f"LOCATION:{_ics_escape(settings.get('address') or '')}",
        "BEGIN:VALARM",
        "TRIGGER:-PT15M",
        "ACTION:DISPLAY",
        "DESCRIPTION:תזכורת לתור",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def local_booking_time(booking: dict) -> datetime:
    return datetime.fromtimestamp(booking["starts_at"], ZoneInfo(config.TZ_NAME))
