"""Lossless date parsing for REQ-IN-12."""

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal


@dataclass(frozen=True)
class ParsedDate:
    value: date | None
    status: Literal["valid", "blank", "unparseable"]
    raw: object
    warning: str | None = None


def parse_date(value: object) -> ParsedDate:
    """REQ-IN-12: parse UK dates/Excel serials without raising on invalid input.

    Excel's fictitious serial 60 maps to 1900-02-28 with a warning.
    Supported serials span 1 through 2958465 (9999-12-31).
    """
    if value is None or isinstance(value, str) and not value.strip():
        return ParsedDate(None, "blank", value)
    if isinstance(value, datetime):
        return ParsedDate(value.date(), "valid", value)
    if isinstance(value, date):
        return ParsedDate(value, "valid", value)
    try:
        if isinstance(value, bool):
            raise ValueError("Boolean is not a date")
        if isinstance(value, (int, float)) or (
            isinstance(value, str) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip())
        ):
            serial = float(value)
            if not math.isfinite(serial) or not 1 <= serial < 2958466:
                raise ValueError("Excel serial outside supported range")
            days = int(serial)
            parsed = date(1899, 12, 31) + timedelta(days=days - (days >= 60))
            warning = "Excel serial 60 represents fictitious 1900-02-29" if days == 60 else None
            return ParsedDate(parsed, "valid", value, warning)
        if isinstance(value, str):
            text = value.strip()
            weekday = re.match(r"^([A-Za-z]+),?\s+(?=\d)", text)
            expected = None
            if weekday and weekday[1][:3].lower() in (
                "mon",
                "tue",
                "wed",
                "thu",
                "fri",
                "sat",
                "sun",
            ):
                expected = weekday[1][:3].lower()
                text = text[weekday.end() :]
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y"):
                try:
                    parsed = datetime.strptime(text, fmt).date()
                except ValueError:
                    continue
                warning = None
                if expected and parsed.strftime("%a").lower() != expected:
                    warning = "Weekday does not match parsed day-first date"
                return ParsedDate(parsed, "valid", value, warning)
    except (ValueError, OverflowError):
        pass
    return ParsedDate(None, "unparseable", value)
