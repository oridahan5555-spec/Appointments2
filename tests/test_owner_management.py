import auth
import db
from tests.helpers import booking_payload, csrf_headers, future_open_day


def activate(client, session):
    client.cookies.set(
        auth.COOKIE,
        session["raw_token"],
        domain="testserver.local",
        path="/",
    )


def test_owner_service_lifecycle(client, session_factory):
    owner = session_factory(role="owner")
    headers = csrf_headers(owner)

    created = client.post(
        "/api/owner/services",
        headers=headers,
        json={
            "name": "Nail treatment",
            "category": "Nails",
            "price": 150,
            "duration_minutes": 45,
            "is_active": True,
            "display_order": 2,
        },
    )
    service_id = created.json()["id"]
    updated = client.put(
        f"/api/owner/services/{service_id}",
        headers=headers,
        json={
            "name": "Updated treatment",
            "category": None,
            "price": 175,
            "duration_minutes": 60,
            "is_active": False,
            "display_order": 3,
        },
    )
    listed = client.get("/api/owner/services")
    deleted = client.delete(f"/api/owner/services/{service_id}", headers=headers)

    assert created.status_code == 200
    assert updated.status_code == 200
    saved = next(item for item in listed.json()["services"] if item["id"] == service_id)
    assert saved["name"] == "Updated treatment"
    assert saved["price"] == 175
    assert saved["is_active"] == 0
    assert deleted.status_code == 200


def test_owner_hours_overrides_and_blocks_lifecycle(client, session_factory):
    owner = session_factory(role="owner")
    headers = csrf_headers(owner)
    hours = [
        {
            "day_of_week": day,
            "is_closed": day == 6,
            "open_time": None if day == 6 else "08:30",
            "close_time": None if day == 6 else "17:30",
            "slot_interval_minutes": 30,
        }
        for day in range(7)
    ]

    saved_hours = client.put("/api/owner/hours", headers=headers, json=hours)
    day = future_open_day(10)
    override = client.post(
        "/api/owner/overrides",
        headers=headers,
        json={
            "override_date": day,
            "is_closed": False,
            "open_time": "10:00",
            "close_time": "14:00",
            "slot_interval_minutes": 20,
            "internal_note": "Private note",
        },
    )
    block = client.post(
        "/api/owner/blocks",
        headers=headers,
        json={
            "blocked_date": day,
            "blocked_time": "11:00",
            "duration_minutes": 40,
            "internal_note": "Meeting",
        },
    )
    block_id = block.json()["id"]

    assert saved_hours.status_code == 200
    assert override.status_code == 200
    assert block.status_code == 200
    assert client.get("/api/owner/hours").json()["hours"][0]["open_time"] == "08:30"
    assert len(client.get("/api/owner/overrides").json()["overrides"]) == 1
    assert len(client.get("/api/owner/blocks").json()["blocks"]) == 1
    assert client.delete(f"/api/owner/overrides/{day}", headers=headers).status_code == 200
    assert client.delete(f"/api/owner/blocks/{block_id}", headers=headers).status_code == 200


def test_settings_validation_and_public_response_do_not_expose_private_config(
    client, session_factory
):
    owner = session_factory(role="owner")
    headers = csrf_headers(owner)
    invalid = client.put(
        "/api/owner/settings",
        headers=headers,
        json={
            "name": "Business",
            "phone": "not-a-phone",
            "social_url": "javascript:alert(1)",
            "min_lead_minutes": 30,
            "max_days_ahead": 90,
        },
    )
    valid = client.put(
        "/api/owner/settings",
        headers=headers,
        json={
            "name": "Professional Business",
            "description": "Appointments",
            "address": "Main Street 1",
            "phone": "+972 50-123-4567",
            "social_url": "https://example.com/social",
            "waze_url": "https://example.com/waze",
            "cover_image": None,
            "profile_image": None,
            "preparation_message": "Arrive five minutes early",
            "min_lead_minutes": 30,
            "max_days_ahead": 90,
        },
    )
    public = client.get("/api/business")

    assert invalid.status_code == 422
    assert valid.status_code == 200
    assert public.json()["settings"]["name"] == "Professional Business"
    serialized = public.text
    assert "OWNER_EMAIL" not in serialized
    assert "MAILJET" not in serialized
    assert "google_refresh_token" not in serialized
    assert "internal_note" not in serialized


def test_owner_can_block_customer_and_customer_cannot_book(client, session_factory):
    customer = session_factory(email="blocked@example.com")
    owner = session_factory(role="owner")
    headers = csrf_headers(owner)
    updated = client.put(
        f"/api/owner/customers/{customer['customer_id']}",
        headers=headers,
        json={"internal_note": "Do not accept online", "is_blocked": True},
    )
    activate(client, customer)
    blocked = client.post("/api/bookings", headers=csrf_headers(customer), json=booking_payload())

    assert updated.status_code == 200
    assert blocked.status_code == 403
    with db.get_conn() as conn:
        saved = db.customer_by_id(conn, customer["customer_id"])
    assert saved["is_blocked"] == 1
    assert saved["internal_note"] == "Do not accept online"


def test_owner_filters_reject_invalid_ranges_and_status(client, session_factory):
    session_factory(role="owner")

    invalid_status = client.get("/api/owner/bookings?status=' OR 1=1 --")
    backwards = client.get("/api/owner/bookings?date_from=2026-08-01&date_to=2026-07-01")

    assert invalid_status.status_code == 400
    assert backwards.status_code == 400
