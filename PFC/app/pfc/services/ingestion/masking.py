"""REQ-IN-23 / REQ-OUT-03: phone detection and masked evidence for CV responses."""

import re
from collections.abc import Iterator

from .browse import _mask

PHONE = re.compile(r"(?<![\w@])\+?(?:\(\d{1,4}\)|\d)[\d ().-]{5,}\d(?!\w)")
MASK_PHONE = re.compile(r"(?<![\w@])\+?(?:\(\d{1,4}\)|\d)[\d +().–—/-]{5,}\d(?!\w)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+")
YEAR_RANGE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[-–—/]\s*(?:19|20)\d{2}(?!\d)")


def _joined_to_digits(text: str, start: int, end: int) -> bool:
    left, right = start - 1, end
    while left >= 0 and text[left] in " +().-":
        left -= 1
    while right < len(text) and text[right] in " +().-":
        right += 1
    return (left >= 0 and text[left].isdigit()) or (right < len(text) and text[right].isdigit())


def _phone_candidates(text: str, *, conservative: bool = False) -> Iterator[re.Match[str]]:
    start = 0
    segments = []
    for years in YEAR_RANGE.finditer(text):
        if conservative and _joined_to_digits(text, years.start(), years.end()):
            continue
        segments.append((start, years.start()))
        start = years.end()
    segments.append((start, len(text)))
    for start, end in segments:
        yield from (MASK_PHONE if conservative else PHONE).finditer(text, start, end)


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
    for match in reversed(list(_phone_candidates(text, conservative=True))):
        if len(re.sub(r"\D", "", match[0])) >= 7:
            text = text[: match.start()] + (mask_phone(match[0]) or "***") + text[match.end() :]
    return text
