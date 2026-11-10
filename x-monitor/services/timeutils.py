from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def parse_timezone_input(value: str) -> str | None:
    """Accepts IANA names (Europe/Moscow) or fixed offsets (UTC+3, +03:00, GMT-5)."""
    value = value.strip()
    if not value:
        return None
    if "/" in value or value.upper() == "UTC":
        # IANA names are case sensitive; try a case-insensitive match.
        if is_valid_tz(value):
            return value
        from zoneinfo import available_timezones

        lowered = value.lower()
        for tz in available_timezones():
            if tz.lower() == lowered:
                return tz
        return None
    raw = value.upper().replace("UTC", "").replace("GMT", "").strip()
    if not raw:
        return "UTC"
    sign = 1
    if raw[0] in "+-":
        sign = -1 if raw[0] == "-" else 1
        raw = raw[1:]
    hours_part = raw.split(":")[0]
    if not hours_part.isdigit():
        return None
    hours = int(hours_part)
    minutes = raw.split(":")[1] if ":" in raw else "0"
    if not minutes.isdigit() or int(minutes) != 0 or hours > 14:
        return None  # Etc/GMT zones only support whole hours
    if hours == 0:
        return "UTC"
    # POSIX-style names have the inverted sign: UTC+3 == Etc/GMT-3.
    return f"Etc/GMT{'-' if sign > 0 else '+'}{hours}"


def describe_tz(name: str) -> str:
    offset = datetime.now(get_tz(name)).utcoffset() or timedelta()
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{name} (UTC{sign}{total // 60:02d}:{total % 60:02d})"


def utcnow() -> datetime:
    return datetime.now(UTC)


def format_dt(dt: datetime | None, tz_name: str, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(get_tz(tz_name)).strftime(fmt)


def is_quiet_time(now: datetime, tz_name: str, enabled: bool, start_hour: int, end_hour: int) -> bool:
    if not enabled or start_hour == end_hour:
        return False
    hour = now.astimezone(get_tz(tz_name)).hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour  # window crosses midnight


def start_of_day(now: datetime, tz_name: str) -> datetime:
    local = now.astimezone(get_tz(tz_name))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def period_start(period: str, now: datetime, tz_name: str) -> datetime | None:
    if period == "day":
        return start_of_day(now, tz_name)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    return None


def parse_date_range(text: str, tz_name: str) -> tuple[datetime, datetime] | None:
    """'01.11.2026-10.11.2026' or a single date '05.11.2026' -> [from, to) in UTC."""
    parts = [p.strip() for p in text.replace("—", "-").replace("–", "-").split("-") if p.strip()]
    if not 1 <= len(parts) <= 2:
        return None
    tz = get_tz(tz_name)
    try:
        dates = [datetime.strptime(p, "%d.%m.%Y").replace(tzinfo=tz) for p in parts]
    except ValueError:
        return None
    start = dates[0]
    end = dates[-1] + timedelta(days=1)
    if end <= start:
        return None
    return start.astimezone(UTC), end.astimezone(UTC)
