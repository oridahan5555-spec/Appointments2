import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db

TZ = ZoneInfo(config.TZ_NAME)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def validate_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("invalid date")
    date.fromisoformat(value)
    return value


def parse_minutes(value: str) -> int:
    if not TIME_RE.fullmatch(value):
        raise ValueError("invalid time")
    hours, minutes = map(int, value.split(":"))
    if hours > 23 or minutes > 59:
        raise ValueError("invalid time")
    return hours * 60 + minutes


def validate_time(value: str) -> str:
    parse_minutes(value)
    return value


def time_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def to_unix(day: str, time_s: str, duration: int):
    validate_date(day)
    validate_time(time_s)
    if duration < 1 or duration > 480:
        raise ValueError("invalid duration")
    naive = datetime.fromisoformat(f"{day}T{time_s}:00")
    candidates = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=TZ, fold=fold)
        round_trip = aware.astimezone(UTC).astimezone(TZ).replace(tzinfo=None)
        if round_trip == naive:
            candidates.append(aware)
    if not candidates:
        raise ValueError("nonexistent local time")
    if len(candidates) == 2 and candidates[0].utcoffset() != candidates[1].utcoffset():
        raise ValueError("ambiguous local time")
    starts = int(candidates[0].timestamp())
    return starts, starts + duration * 60


def local_today() -> date:
    return datetime.now(TZ).date()


def weekday_sun0(day: date) -> int:
    return (day.weekday() + 1) % 7


def hours_for(day_s: str, hours, overrides):
    override = overrides.get(day_s)
    if override and override["is_closed"]:
        return None
    if override and override["open_time"]:
        return {
            "is_closed": 0,
            "open_time": override["open_time"],
            "close_time": override["close_time"],
            "slot_interval_minutes": override["slot_interval_minutes"] or 15,
        }
    hours_row = hours[weekday_sun0(date.fromisoformat(day_s))]
    return None if hours_row["is_closed"] else hours_row


def within_working_hours(conn, day_s, time_s, duration):
    validate_date(day_s)
    validate_time(time_s)
    hours = {h["day_of_week"]: h for h in db.working_hours(conn)}
    overrides = {o["override_date"]: o for o in db.overrides_between(conn, day_s, day_s)}
    hours_row = hours_for(day_s, hours, overrides)
    if not hours_row or not hours_row["open_time"] or not hours_row["close_time"]:
        return False
    start = parse_minutes(time_s)
    opening = parse_minutes(hours_row["open_time"])
    closing = parse_minutes(hours_row["close_time"])
    interval = int(hours_row.get("slot_interval_minutes") or 15)
    if interval < 5 or interval > 240:
        interval = 15
    return start >= opening and start + duration <= closing and (start - opening) % interval == 0


def available_slots(conn, date_from: str, date_to: str, duration: int):
    validate_date(date_from)
    validate_date(date_to)
    if duration < 1 or duration > 480:
        raise ValueError("invalid duration")
    settings = db.settings(conn)
    start_d, end_d = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if end_d < start_d:
        raise ValueError("invalid date range")
    if (end_d - start_d).days > 31:
        raise ValueError("date range is too large")
    today = local_today()
    start_d = max(start_d, today)
    max_end = today + timedelta(days=settings["max_days_ahead"])
    end_d = min(end_d, max_end)
    if start_d > end_d:
        return []
    hours = {h["day_of_week"]: h for h in db.working_hours(conn)}
    overrides = {
        o["override_date"]: o for o in db.overrides_between(conn, date_from, end_d.isoformat())
    }
    min_start = db.now() + settings["min_lead_minutes"] * 60
    out = []
    day = start_d
    while day <= end_d:
        day_s = day.isoformat()
        hours_row = hours_for(day_s, hours, overrides)
        day_slots = []
        if hours_row and hours_row["open_time"] and hours_row["close_time"]:
            t = parse_minutes(hours_row["open_time"])
            close = parse_minutes(hours_row["close_time"])
            step = int(hours_row["slot_interval_minutes"])
            if step < 5 or step > 240:
                step = 15
            while t + duration <= close:
                time_s = time_text(t)
                try:
                    starts, ends = to_unix(day_s, time_s, duration)
                except ValueError:
                    t += step
                    continue
                if (
                    starts >= min_start
                    and not db.booking_overlap(conn, starts, ends)
                    and not db.block_overlap(conn, starts, ends)
                ):
                    day_slots.append(time_s)
                t += step
        if day_slots:
            out.append({"date": day_s, "times": day_slots})
        day += timedelta(days=1)
    return out
