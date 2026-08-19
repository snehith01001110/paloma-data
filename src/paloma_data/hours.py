from __future__ import annotations

from datetime import date, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DAY_INDEX = {
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
}


class HoursFormatError(ValueError):
    """The supplied schedule cannot be represented without guessing."""


def normalize_hours(
    value: Any,
    *,
    timezone_name: str = "America/Los_Angeles",
) -> dict[str, Any] | None:
    """Return Paloma's versioned schedule format.

    Accepted inputs are either the canonical format or the day-to-interval mapping already
    emitted by the licensed Foursquare adapter. Provider-native strings are intentionally
    rejected: parsing those requires a reviewed, source-specific parser and storage policy.
    """
    if value in (None, "", {}, []):
        return None
    _validate_timezone(timezone_name)
    if not isinstance(value, dict):
        raise HoursFormatError("hours must be a structured object")

    if value.get("schema_version") == "paloma-hours-v1":
        timezone_name = str(value.get("timezone") or timezone_name)
        _validate_timezone(timezone_name)
        weekly = _canonical_weekly(value.get("weekly") or [])
        special = _canonical_special(value.get("special") or [])
    else:
        weekly = _provider_weekly(value)
        special = []

    if not weekly and not special:
        return None
    return {
        "schema_version": "paloma-hours-v1",
        "timezone": timezone_name,
        "weekly": weekly,
        "special": special,
    }


def _provider_weekly(value: dict[str, Any]) -> list[dict[str, Any]]:
    weekly: list[dict[str, Any]] = []
    for raw_day, intervals in value.items():
        day = DAY_INDEX.get(str(raw_day).casefold())
        if day is None:
            if raw_day in {"regular", "special", "timezone", "schema_version"}:
                continue
            raise HoursFormatError(f"unknown weekday: {raw_day}")
        if not isinstance(intervals, list):
            raise HoursFormatError(f"intervals for {raw_day} must be a list")
        for interval in intervals:
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise HoursFormatError(f"invalid interval for {raw_day}")
            opens = _clock(interval[0])
            closes, explicit_offset = _closing_clock(interval[1])
            offset = explicit_offset if explicit_offset is not None else int(closes <= opens)
            weekly.append(
                {
                    "day": day,
                    "opens": opens,
                    "closes": closes,
                    "closes_day_offset": offset,
                }
            )
    return _sort_weekly(weekly)


def _canonical_weekly(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise HoursFormatError("weekly hours must be a list")
    weekly: list[dict[str, Any]] = []
    for interval in value:
        if not isinstance(interval, dict):
            raise HoursFormatError("weekly interval must be an object")
        try:
            day = int(interval["day"])
            offset = int(interval.get("closes_day_offset", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise HoursFormatError("weekly interval is missing a valid day") from exc
        if day not in range(1, 8) or offset not in {0, 1}:
            raise HoursFormatError("weekday or closing-day offset is out of range")
        weekly.append(
            {
                "day": day,
                "opens": _clock(interval.get("opens")),
                "closes": _clock(interval.get("closes")),
                "closes_day_offset": offset,
            }
        )
    return _sort_weekly(weekly)


def _canonical_special(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise HoursFormatError("special hours must be a list")
    special: list[dict[str, Any]] = []
    for interval in value:
        if not isinstance(interval, dict):
            raise HoursFormatError("special interval must be an object")
        try:
            service_date = date.fromisoformat(str(interval["date"]))
        except (KeyError, ValueError) as exc:
            raise HoursFormatError("special interval requires an ISO date") from exc
        closed = bool(interval.get("closed", False))
        normalized: dict[str, Any] = {
            "date": service_date.isoformat(),
            "closed": closed,
        }
        if not closed:
            opens = _clock(interval.get("opens"))
            closes = _clock(interval.get("closes"))
            offset = int(interval.get("closes_day_offset", int(closes <= opens)))
            if offset not in {0, 1}:
                raise HoursFormatError("special closing-day offset is out of range")
            normalized.update(
                opens=opens,
                closes=closes,
                closes_day_offset=offset,
            )
        special.append(normalized)
    return sorted(special, key=lambda item: (item["date"], item.get("opens", "")))


def _sort_weekly(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = {
        (item["day"], item["opens"], item["closes"], item["closes_day_offset"]): item
        for item in value
    }
    return sorted(
        deduped.values(),
        key=lambda item: (
            item["day"],
            item["opens"],
            item["closes"],
            item["closes_day_offset"],
        ),
    )


def _clock(value: Any) -> str:
    raw = str(value or "").strip()
    if raw == "24:00":
        return "00:00"
    try:
        parsed = time.fromisoformat(raw)
    except ValueError as exc:
        raise HoursFormatError(f"invalid clock time: {raw}") from exc
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise HoursFormatError("clock times must use local HH:MM precision")
    return parsed.strftime("%H:%M")


def _closing_clock(value: Any) -> tuple[str, int | None]:
    raw = str(value or "").strip()
    if raw.startswith("+"):
        return _clock(raw[1:]), 1
    if raw == "24:00":
        return "00:00", 1
    return _clock(raw), None


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise HoursFormatError(f"unknown IANA timezone: {value}") from exc
