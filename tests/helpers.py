from datetime import timedelta

import availability


def future_open_day(days: int = 2) -> str:
    day = availability.local_today() + timedelta(days=days)
    while availability.weekday_sun0(day) == 6:
        day += timedelta(days=1)
    return day.isoformat()


def booking_payload(
    *,
    day: str | None = None,
    time: str = "10:00",
    name: str | None = None,
    notes: str | None = None,
    service_ids: list[int] | None = None,
) -> dict:
    payload = {
        "service_ids": service_ids or [1],
        "date": day or future_open_day(),
        "time": time,
    }
    if name is not None:
        payload["name"] = name
    if notes is not None:
        payload["notes"] = notes
    return payload


def csrf_headers(session: dict) -> dict[str, str]:
    return {"X-CSRF-Token": session["csrf"]}
