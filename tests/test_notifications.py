import json

import availability
import config
import db
import mailer
import notifications
from tests.helpers import booking_payload, csrf_headers, future_open_day


def make_approved_booking(days=3) -> dict:
    day = future_open_day(days)
    starts, ends = availability.to_unix(day, "10:00", 30)
    with db.get_conn() as conn, db.transaction(conn, immediate=True):
        customer_id = db.upsert_customer(conn, "reminder@example.com", "Reminder Customer")
        services = db.active_services_by_ids(conn, [1])
        booking_id = db.insert_booking(
            conn,
            customer_id,
            [1],
            services,
            day,
            "10:00",
            None,
            starts,
            ends,
        )
        conn.execute("UPDATE bookings SET status='approved' WHERE id=?", (booking_id,))
        return db.booking_with_customer(conn, booking_id)


def test_approval_schedules_customer_and_owner_reminders_at_24h_and_3h():
    booking = make_approved_booking()
    with db.get_conn() as conn:
        notifications.queue_booking_approved(conn, booking)
        jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM notification_jobs WHERE booking_id=? ORDER BY kind,recipient_kind",
                (booking["id"],),
            )
        ]

    reminders = [job for job in jobs if job["kind"].startswith("reminder_")]
    assert len(reminders) == 4
    assert {job["recipient_kind"] for job in reminders} == {"customer", "owner"}
    for job in reminders:
        before = 24 * 3600 if job["kind"] == "reminder_24h" else 3 * 3600
        assert job["scheduled_at"] == booking["starts_at"] - before


def test_due_reminders_are_delivered_exactly_once(sent_emails):
    booking = make_approved_booking()
    with db.get_conn() as conn:
        notifications.queue_booking_approved(conn, booking)
        conn.execute(
            "UPDATE notification_jobs SET scheduled_at=?,next_attempt_at=? "
            "WHERE booking_id=? AND kind IN ('reminder_24h','reminder_3h')",
            (db.now(), db.now(), booking["id"]),
        )

    first = notifications.process_due_jobs(limit=20)
    second = notifications.process_due_jobs(limit=20)
    reminder_messages = [
        message for message in sent_emails if message["template"].startswith("reminder_")
    ]

    assert first["sent"] == 5
    assert second["claimed"] == 0
    assert len(reminder_messages) == 4
    assert {message["recipient"] for message in reminder_messages} == {
        "reminder@example.com",
        config.OWNER_EMAIL,
    }
    assert all("TRIGGER:-PT15M" in message["ics_content"] for message in reminder_messages)


def test_failed_email_retries_and_then_stops_after_success(monkeypatch):
    booking = make_approved_booking()
    outcomes = iter(["network:TimeoutError", "mailjet:200"])
    monkeypatch.setattr(mailer, "send_email", lambda *_args, **_kwargs: next(outcomes))
    with db.get_conn() as conn:
        notifications._queue_job(
            conn,
            booking["id"],
            "booking_approved",
            "customer",
            db.now(),
        )

    failed = notifications.process_due_jobs(limit=1)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE notification_jobs SET next_attempt_at=? WHERE booking_id=?",
            (db.now(), booking["id"]),
        )
    sent = notifications.process_due_jobs(limit=1)
    final = notifications.process_due_jobs(limit=1)

    assert failed == {"claimed": 1, "sent": 0, "failed": 1, "cancelled": 0}
    assert sent == {"claimed": 1, "sent": 1, "failed": 0, "cancelled": 0}
    assert final["claimed"] == 0
    with db.get_conn() as conn:
        job = conn.execute(
            "SELECT * FROM notification_jobs WHERE booking_id=?", (booking["id"],)
        ).fetchone()
    assert job["status"] == "sent"
    assert job["attempts"] == 2


def test_cancellation_prevents_pending_reminders_from_sending(sent_emails):
    booking = make_approved_booking()
    with db.get_conn() as conn:
        notifications.queue_booking_approved(conn, booking)
        notifications.queue_booking_cancelled(
            conn,
            booking["id"],
            notify_customer=False,
            notify_owner=False,
        )
        conn.execute(
            "UPDATE notification_jobs SET scheduled_at=?,next_attempt_at=? "
            "WHERE booking_id=? AND kind IN ('reminder_24h','reminder_3h')",
            (db.now(), db.now(), booking["id"]),
        )

    notifications.process_due_jobs(limit=20)

    assert not any(message["template"].startswith("reminder_") for message in sent_emails)


def test_reschedule_cancels_old_reminders_that_no_longer_fit_new_time():
    booking = make_approved_booking()
    with db.get_conn() as conn:
        notifications.queue_booking_approved(conn, booking)
        booking["starts_at"] = db.now() + 2 * 3600
        booking["ends_at"] = booking["starts_at"] + 1800
        notifications.queue_booking_rescheduled(conn, booking)
        reminders = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM notification_jobs WHERE booking_id=? "
                "AND kind IN ('reminder_24h','reminder_3h')",
                (booking["id"],),
            )
        ]

    assert len(reminders) == 4
    assert {job["status"] for job in reminders} == {"cancelled"}


def test_cron_endpoint_requires_exact_bearer_secret(client):
    missing = client.get("/api/cron/reminders")
    wrong = client.get("/api/cron/reminders", headers={"Authorization": "Bearer wrong"})
    correct = client.get(
        "/api/cron/reminders",
        headers={"Authorization": f"Bearer {config.CRON_SECRET}"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert correct.status_code == 200
    assert correct.json()["ok"] is True


def test_ics_escapes_crlf_and_contains_stable_uid_and_alarm():
    booking = make_approved_booking()
    booking["services_snapshot"] = json.dumps(
        [{"name": "Service\r\nATTENDEE:attacker@example.com"}]
    )
    settings = {"name": "Business", "address": "Main\r\nX-HEADER: injected"}

    content = notifications.calendar_file(booking, settings)

    assert f"UID:booking-{booking['id']}@appointments" in content
    assert "TRIGGER:-PT15M" in content
    assert "Service\\nATTENDEE:attacker@example.com" in content
    assert "Main\\nX-HEADER: injected" in content
    assert "\r\nATTENDEE:attacker@example.com\r\n" not in content


def test_arrival_request_and_customer_answer_flow(client, session_factory, sent_emails):
    customer = session_factory(email="arrival@example.com")
    booking_id = client.post(
        "/api/bookings",
        headers=csrf_headers(customer),
        json=booking_payload(),
    ).json()["id"]
    owner = session_factory(role="owner")
    client.post(
        f"/api/owner/bookings/{booking_id}/status",
        headers=csrf_headers(owner),
        json={"status": "approved"},
    )
    sent_emails.clear()

    requested = client.post(
        f"/api/owner/bookings/{booking_id}/request-arrival",
        headers=csrf_headers(owner),
    )
    repeated = client.post(
        f"/api/owner/bookings/{booking_id}/request-arrival",
        headers=csrf_headers(owner),
    )
    client.cookies.set(
        "booking_session",
        customer["raw_token"],
        domain="testserver.local",
        path="/",
    )
    answered = client.post(
        f"/api/bookings/{booking_id}/arrival",
        headers=csrf_headers(customer),
        json={"answer": "confirmed"},
    )

    assert requested.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["already_applied"] is True
    assert answered.status_code == 200
    assert [message["template"] for message in sent_emails] == ["arrival_requested"]
    with db.get_conn() as conn:
        status = conn.execute(
            "SELECT arrival_status FROM bookings WHERE id=?", (booking_id,)
        ).fetchone()["arrival_status"]
    assert status == "confirmed"
