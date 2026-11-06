"""REQ-IN-23 / REQ-OUT-03: phone detection and masked evidence for CV responses."""

import re
from collections.abc import Iterator

from .browse import _mask

PHONE = re.compile(r"(?<![\w@])\+?(?:\(\d{1,4}\)|\d)[\d ().-]{5,}\d(?!\w)")
MASK_PHONE = re.compile(r"(?<![\w@])\+?(?:\(\d{1,4}\)|\d)[\d +().,;–—/-]{5,}\d(?!\w)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")
YEAR_RANGE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[-–—/]\s*(?:19|20)\d{2}(?!\d)")
YEAR_LIST = re.compile(r"(?:19|20)\d{2}(?:[ /,;–—-]+(?:19|20)\d{2})*")


def _phone_candidates(text: str) -> Iterator[re.Match[str]]:
    start = 0
    segments = []
    for years in YEAR_RANGE.finditer(text):
        segments.append((start, years.start()))
        start = years.end()
    segments.append((start, len(text)))
    for start, end in segments:
        yield from PHONE.finditer(text, start, end)


def phone_matches(text: str) -> Iterator[re.Match[str]]:
    for match in _phone_candidates(text):
        value = match[0]
        if 7 <= len(re.sub(r"\D", "", value)) <= 15 and not re.fullmatch(
            r"\d{4}[-.]\d{4}|\d{4}[-.]\d{2}[-.]\d{2}|\d{2}[-.]\d{2}[-.]\d{4}", value
        ):
            yield match


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    return "***" + phone[-2:] if len(phone) > 2 else "***"


def mask_evidence(text: str) -> str:
    text = EMAIL.sub(lambda match: _mask(match[0]) or "***", text)
    for match in reversed(list(MASK_PHONE.finditer(text))):
        if not YEAR_LIST.fullmatch(match[0]) and len(re.sub(r"\D", "", match[0])) >= 7:
            text = text[: match.start()] + (mask_phone(match[0]) or "***") + text[match.end() :]
    return text
