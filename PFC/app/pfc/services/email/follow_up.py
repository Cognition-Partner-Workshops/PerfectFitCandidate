from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo

DEFAULTS: dict[str, object] = {
    "email.follow_up.enabled": True,
    "email.follow_up.time": "09:00",
    "email.follow_up.holidays": [],
}


def validate_settings(values: dict[str, object]) -> None:
    for key, value in values.items():
        if key == "email.follow_up.enabled" and type(value) is not bool:
            raise ValueError("Follow-up enabled must be a boolean")
        if key == "email.follow_up.time" and (
            not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
        ):
            raise ValueError("Follow-up time must be HH:MM")
        if key == "email.follow_up.holidays":
            if not isinstance(value, list):
                raise ValueError("Follow-up holidays must be ISO dates")
            for day in value:
                if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    raise ValueError("Follow-up holidays must be ISO dates")
                date.fromisoformat(day)


def next_business_day(
    sent_at: datetime,
    local_tz: tzinfo | None = None,
    reminder_time: str = "09:00",
    holidays: list[str] | None = None,
) -> datetime:
    if sent_at.tzinfo is None:
        raise ValueError("sent_at must be timezone-aware")
    validate_settings(
        {"email.follow_up.time": reminder_time, "email.follow_up.holidays": holidays or []}
    )
    if local_tz is None:
        day = sent_at.astimezone().date() + timedelta(days=1)
    else:
        day = sent_at.astimezone(local_tz).date() + timedelta(days=1)
    while day.weekday() >= 5 or day.isoformat() in (holidays or []):
        day += timedelta(days=1)
    result = datetime.combine(day, time.fromisoformat(reminder_time), tzinfo=local_tz)
    return result if local_tz is not None else result.astimezone()


def follow_up_key(message_id: int) -> str:
    return f"pfc-email-follow-up-{message_id}"


def build_ics(key: str, subject: str, body: str, due: datetime, now: datetime) -> bytes:
    def escape(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", r"\n")
            .replace(";", r"\;")
            .replace(",", r"\,")
        )

    def utc(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PFC//Follow-up//EN",
        "BEGIN:VEVENT",
        f"UID:{escape(key)}@pfc",
        f"DTSTAMP:{utc(now)}",
        f"DTSTART:{utc(due)}",
        f"DTEND:{utc(due + timedelta(minutes=15))}",
        "TRANSP:TRANSPARENT",
        f"SUMMARY:{escape(subject)}",
        f"DESCRIPTION:{escape(body)}",
        "BEGIN:VALARM",
        "TRIGGER:PT0M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{escape(subject)}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    folded = []
    for line in lines:
        chunk = ""
        for char in line:
            if len((chunk + char).encode()) > 75:
                folded.append(chunk)
                chunk = " "
            chunk += char
        folded.append(chunk)
    return ("\r\n".join(folded) + "\r\n").encode()
