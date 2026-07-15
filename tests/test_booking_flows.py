import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient

import app as app_module
import auth
import db
from tests.helpers import booking_payload, csrf_headers, future_open_day


def activate(client, session):
    client.cookies.set(auth.COOKIE, session["raw_token"], domain="testserver.local", path="/")


def create_booking(client, session, **overrides):
    activate(client, session)
    payload = booking_payload(**overrides)
    return client.post("/api/bookings", headers=csrf_headers(session), json=payload)


def test_valid_booking_uses_server_price_duration_and_status(client, session_factory):
    customer = session_factory(existing_customer=False, email="first@example.com")
    payload = booking_payload(name="Dana", notes="Please be gentle")

    response = client.post("/api/bookings", headers=csrf_headers(customer), json=payload)

    assert response.status_code == 200
    with db.get_conn() as conn:
        booking = db.booking_with_customer(conn, response.json()["id"])
    assert booking["price"] == 0
    assert booking["duration_minutes"] == 30
    assert booking["status"] == "pending"
    assert booking["customer_name"] == "Dana"


def test_client_controlled_price_duration_status_and_unknown_fields_are_rejected(
    client, session_factory
):
    customer = session_factory(email="untrusted-fields@example.com")
    payload = booking_payload()
    payload.update({"price": 1, "duration_minutes": 5, "status": "approved", "owner": True})

    response = client.post("/api/bookings", headers=csrf_headers(customer), json=payload)

    assert response.status_code == 422
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM bookings").fetchone()["count"] == 0


def test_invalid_service_off_grid_slot_and_far_date_are_rejected(client, session_factory):
    customer = session_factory()

    invalid_service = create_booking(client, customer, service_ids=[999999])
    off_grid = create_booking(client, customer, time="10:01")
    too_far = create_booking(client, customer, day=future_open_day(70))

    assert invalid_service.status_code == 400
    assert off_grid.status_code == 409
    assert too_far.status_code == 409


def test_double_booking_is_rejected_for_second_customer(client, session_factory):
    first = session_factory(email="first-slot@example.com")
    first_response = create_booking(client, first)
    second = session_factory(email="second-slot@example.com")
    second_response = create_booking(client, second)

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM bookings").fetchone()["count"] == 1


def test_booking_creation_rate_limit_prevents_spam(client, session_factory):
    customer = session_factory(email="booking-spam@example.com")
    times = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30"]

    for time in times:
        assert create_booking(client, customer, time=time).status_code == 200
    limited = create_booking(client, customer, time="13:00")

    assert limited.status_code == 429


def test_concurrent_booking_attempts_allow_exactly_one(client, session_factory, sent_emails):
    first_client = TestClient(app_module.app)
    second_client = TestClient(app_module.app)
    first = session_factory(email="concurrent-one@example.com", target_client=first_client)
    second = session_factory(email="concurrent-two@example.com", target_client=second_client)
    barrier = Barrier(2)

    def submit(target, session):
        barrier.wait(timeout=5)
        return target.post(
            "/api/bookings",
            headers=csrf_headers(session),
            json=booking_payload(),
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(submit, first_client, first),
            executor.submit(submit, second_client, second),
        ]
        statuses = sorted(future.result() for future in futures)

    assert statuses == [200, 409]


def test_database_trigger_blocks_overlap_even_without_route_check(client, session_factory):
    customer = session_factory()
    created = create_booking(client, customer)
    with db.get_conn() as conn:
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id=?", (created.json()["id"],)
        ).fetchone()
        second_customer = db.upsert_customer(conn, "direct@example.com", "Direct")
        services = db.active_services_by_ids(conn, [1])
        try:
            db.insert_booking(
                conn,
                second_customer,
                [1],
                services,
                booking["booking_date"],
                booking["booking_time"],
                None,
                booking["starts_at"],
                booking["ends_at"],
            )
        except sqlite3.IntegrityError as exc:
            assert "booking_overlap" in str(exc)
        else:
            raise AssertionError("database overlap trigger did not run")


def test_booking_idor_does_not_expose_or_cancel_another_customer_booking(client, session_factory):
    owner_of_booking = session_factory(email="owner-of-booking@example.com")
    created = create_booking(client, owner_of_booking)
    booking_id = created.json()["id"]
    other = session_factory(email="other-customer@example.com")
    activate(client, other)

    cancel = client.post(f"/api/bookings/{booking_id}/cancel", headers=csrf_headers(other))
    calendar = client.get(f"/api/bookings/{booking_id}/ics")
    mine = client.get("/api/bookings/mine")

    assert cancel.status_code == 404
    assert calendar.status_code == 404
    assert mine.status_code == 200
    assert mine.json()["bookings"] == []


def test_approval_is_idempotent_and_invalid_transition_is_rejected(client, session_factory):
    customer = session_factory()
    booking_id = create_booking(client, customer).json()["id"]
    owner = session_factory(role="owner")
    activate(client, owner)

    approved = client.post(
        f"/api/owner/bookings/{booking_id}/status",
        headers=csrf_headers(owner),
        json={"status": "approved"},
    )
    repeated = client.post(
        f"/api/owner/bookings/{booking_id}/status",
        headers=csrf_headers(owner),
        json={"status": "approved"},
    )
    rejected_after_approval = client.post(
        f"/api/owner/bookings/{booking_id}/status",
        headers=csrf_headers(owner),
        json={"status": "rejected"},
    )

    assert approved.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["already_applied"] is True
    assert rejected_after_approval.status_code == 400
    with db.get_conn() as conn:
        approval_jobs = conn.execute(
            "SELECT COUNT(*) AS count FROM notification_jobs "
            "WHERE booking_id=? AND kind='booking_approved'",
            (booking_id,),
        ).fetchone()["count"]
        booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    assert approval_jobs == 1
    assert booking["status"] == "approved"


def test_rejection_is_idempotent_and_cancelled_booking_cannot_be_reopened(client, session_factory):
    first_customer = session_factory(email="reject@example.com")
    rejected_id = create_booking(client, first_customer).json()["id"]
    owner = session_factory(role="owner")
    activate(client, owner)

    rejected = client.post(
        f"/api/owner/bookings/{rejected_id}/status",
        headers=csrf_headers(owner),
        json={"status": "rejected"},
    )
    repeated = client.post(
        f"/api/owner/bookings/{rejected_id}/status",
        headers=csrf_headers(owner),
        json={"status": "rejected"},
    )

    second_customer = session_factory(email="cancel@example.com")
    cancelled_id = create_booking(client, second_customer, time="11:00").json()["id"]
    activate(client, owner)
    cancelled = client.post(
        f"/api/owner/bookings/{cancelled_id}/status",
        headers=csrf_headers(owner),
        json={"status": "cancelled"},
    )
    reopen = client.post(
        f"/api/owner/bookings/{cancelled_id}/status",
        headers=csrf_headers(owner),
        json={"status": "approved"},
    )

    assert rejected.status_code == 200
    assert repeated.json()["already_applied"] is True
    assert cancelled.status_code == 200
    assert reopen.status_code == 400


def test_customer_cancellation_notifies_customer_and_owner_once(
    client, session_factory, sent_emails
):
    customer = session_factory(email="cancel-self@example.com")
    booking_id = create_booking(client, customer).json()["id"]
    sent_emails.clear()

    cancelled = client.post(f"/api/bookings/{booking_id}/cancel", headers=csrf_headers(customer))
    repeated = client.post(f"/api/bookings/{booking_id}/cancel", headers=csrf_headers(customer))

    assert cancelled.status_code == 200
    assert repeated.status_code == 400
    templates = sorted(message["template"] for message in sent_emails)
    assert templates == ["booking_cancelled", "owner_booking_cancelled"]


def test_owner_can_reschedule_and_conflicts_are_rejected(client, session_factory, sent_emails):
    customer = session_factory()
    booking_id = create_booking(client, customer).json()["id"]
    owner = session_factory(role="owner")
    activate(client, owner)
    new_day = future_open_day(3)

    moved = client.put(
        f"/api/owner/bookings/{booking_id}/schedule",
        headers=csrf_headers(owner),
        json={"date": new_day, "time": "11:00"},
    )
    sent_emails.clear()
    repeated = client.put(
        f"/api/owner/bookings/{booking_id}/schedule",
        headers=csrf_headers(owner),
        json={"date": new_day, "time": "11:00"},
    )
    block = client.post(
        "/api/owner/blocks",
        headers=csrf_headers(owner),
        json={
            "blocked_date": future_open_day(4),
            "blocked_time": "12:00",
            "duration_minutes": 60,
        },
    )
    blocked_move = client.put(
        f"/api/owner/bookings/{booking_id}/schedule",
        headers=csrf_headers(owner),
        json={"date": future_open_day(4), "time": "12:00"},
    )

    assert moved.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["already_applied"] is True
    assert sent_emails == []
    assert block.status_code == 200
    assert blocked_move.status_code == 409
    with db.get_conn() as conn:
        booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    assert booking["booking_date"] == new_day
    assert booking["booking_time"] == "11:00"


def test_cancelled_booking_cannot_be_rescheduled(client, session_factory):
    customer = session_factory()
    booking_id = create_booking(client, customer).json()["id"]
    owner = session_factory(role="owner")
    activate(client, owner)
    client.post(
        f"/api/owner/bookings/{booking_id}/status",
        headers=csrf_headers(owner),
        json={"status": "cancelled"},
    )

    response = client.put(
        f"/api/owner/bookings/{booking_id}/schedule",
        headers=csrf_headers(owner),
        json={"date": future_open_day(3), "time": "11:00"},
    )

    assert response.status_code == 400


def test_no_show_is_idempotent(client, session_factory):
    customer = session_factory()
    booking_id = create_booking(client, customer).json()["id"]
    owner = session_factory(role="owner")
    activate(client, owner)
    client.post(
        f"/api/owner/bookings/{booking_id}/status",
        headers=csrf_headers(owner),
        json={"status": "approved"},
    )

    first = client.post(f"/api/owner/bookings/{booking_id}/no-show", headers=csrf_headers(owner))
    second = client.post(f"/api/owner/bookings/{booking_id}/no-show", headers=csrf_headers(owner))

    assert first.status_code == 200
    assert second.status_code == 200
    with db.get_conn() as conn:
        count = conn.execute(
            "SELECT no_show_count FROM customers WHERE id=?",
            (customer["customer_id"],),
        ).fetchone()["no_show_count"]
    assert count == 1


def test_hidden_booking_disappears_only_after_cancellation(client, session_factory):
    customer = session_factory()
    booking_id = create_booking(client, customer).json()["id"]

    before_cancel = client.post(f"/api/bookings/{booking_id}/hide", headers=csrf_headers(customer))
    client.post(f"/api/bookings/{booking_id}/cancel", headers=csrf_headers(customer))
    hidden = client.post(f"/api/bookings/{booking_id}/hide", headers=csrf_headers(customer))

    assert before_cancel.status_code == 400
    assert hidden.status_code == 200
    assert client.get("/api/bookings/mine").json()["bookings"] == []


def test_malicious_text_is_stored_as_data_and_sql_injection_does_not_execute(
    client, session_factory
):
    payload_name = "<script>alert(1)</script>"
    payload_notes = "'); DROP TABLE bookings; --<img src=x onerror=alert(1)>"
    customer = session_factory(existing_customer=False, email="xss@example.com")

    response = create_booking(client, customer, name=payload_name, notes=payload_notes)

    assert response.status_code == 200
    with db.get_conn() as conn:
        booking = db.booking_with_customer(conn, response.json()["id"])
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bookings'"
        ).fetchone()
    assert booking["customer_name"] == payload_name
    assert booking["notes"] == payload_notes
    assert table_exists is not None


def test_database_foreign_keys_and_status_transition_trigger_are_active(client, session_factory):
    customer = session_factory()
    booking_id = create_booking(client, customer).json()["id"]
    with db.get_conn() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        try:
            conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
            conn.execute("UPDATE bookings SET status='approved' WHERE id=?", (booking_id,))
        except sqlite3.IntegrityError as exc:
            assert "invalid_booking_status_transition" in str(exc)
        else:
            raise AssertionError("illegal status transition was accepted")
