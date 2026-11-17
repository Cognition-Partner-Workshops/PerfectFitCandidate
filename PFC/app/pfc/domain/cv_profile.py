"""Deterministic whole-CV evidence; no extraction service or wall-clock dependency."""

import re
from dataclasses import dataclass
from datetime import date

from .normalise import SynonymTable, normalise_term


def skill_hits(text: str, synonyms: SynonymTable) -> tuple[tuple[str, str, str], ...]:
    """Return one dictionary canonical hit and its source line, never substrings."""
    terms = {
        term
        for row in synonyms.rows
        if row.status == "approved"
        for term in synonyms.expand(row.canonical)
    }
    hits: dict[str, tuple[str, str, str]] = {}
    patterns = [
        (term, re.compile(r"(?<![\w+#.])" + re.escape(term) + r"(?![\w+#])", re.I))
        for term in sorted(terms, key=lambda value: (-len(value), value))
    ]
    for line in text.splitlines():
        for term, pattern in patterns:
            match = pattern.search(line)
            canonical = synonyms.resolve(term)
            if match and canonical:
                start = max(0, match.start() - 90)
                quote = line[start : max(match.end(), start + 200)]
                if start:
                    quote = "…" + quote
                if start + 200 < len(line) and match.end() < len(line):
                    quote += "…"
                hits.setdefault(normalise_term(canonical), (match[0], canonical, quote))
    return tuple(hits.values())


_AMOUNT = r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b"
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE = rf"(?:{_MONTH}\.?\s+)?(?:19|20)\d{{2}}"
_RANGE = re.compile(rf"({_DATE})\s*(?:[-–—]|to)\s*({_DATE}|present|current|till date)\b", re.I)


def _month_index(value: str, as_of: date) -> int:
    if re.fullmatch(r"present|current|till date", value, re.I):
        return as_of.year * 12 + as_of.month - 1
    year = int(value[-4:])
    months = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    month = months.index(value[:3].lower()) if value[:3].isalpha() else 0
    return year * 12 + month


def experience_years(text: str, as_of: date) -> float | None:
    """Explicit total > merged date ranges > summed stated role durations.

    Present/current/till date use the frozen run date. Invalid/future ranges and
    negated durations are ignored. This is overall, not specialist experience.
    """
    clean = "\n".join(
        line
        for line in text.splitlines()
        if not re.search(r"\b(?:no|not|less than|up to|under)\b", line, re.I)
    )
    totals = re.findall(
        _AMOUNT + r"\s+(?:of\s+)?(?:(?:overall|total|professional)\s+)?experience\b", clean, re.I
    )
    totals += re.findall(r"\b(?:overall|total)\s+experience\s*(?:of|:)?\s*" + _AMOUNT, clean, re.I)
    if totals:
        return max(map(float, totals))
    now = as_of.year * 12 + as_of.month - 1
    ranges = sorted(
        (start, end)
        for match in _RANGE.finditer(clean)
        if (start := _month_index(match[1], as_of)) < (end := _month_index(match[2], as_of)) <= now
    )
    if ranges:
        merged: list[tuple[int, int]] = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return sum(end - start for start, end in merged) / 12
    durations = re.findall(_AMOUNT, clean, re.I)
    return sum(map(float, durations)) if durations else None


@dataclass(frozen=True)
class Degree:
    level: int
    technical: bool

    @property
    def label(self) -> str:
        return ("Bachelor", "Master", "Doctorate")[self.level - 1]


_LEVELS = (
    r"\b(?:bachel?ors?|bachler|batchelor|bacholar|b\.?\s*(?:tech|e|sc|s|a)\.?)\b",
    r"\b(?:masters?|m\.?\s*(?:tech|ca|sc|ba)\.?)\b",
    r"\b(?:ph\.?d|doctorate)\b",
)
_TECHNICAL = (
    r"\b(?:technology|engineering|engg|computer\s+(?:science|applications)|"
    r"information\s+technology|IT|[bm]\.?\s*(?:tech|e)\.?|mca)\b"
)


def degree(text: str) -> Degree | None:
    """Classify explicit degree lines only; unrelated technical work is not a degree."""
    found = []
    for line in text.splitlines():
        if re.search(r"\b(?:no|not|pursuing|incomplete|studying)\b", line, re.I):
            continue
        for level, pattern in enumerate(_LEVELS, 1):
            if re.search(pattern, line, re.I):
                found.append(Degree(level, bool(re.search(_TECHNICAL, line, re.I))))
    return max(found, key=lambda value: (value.level, value.technical)) if found else None


def requirement_years(text: str) -> float | None:
    match = re.search(_AMOUNT, text, re.I)
    return float(match[1]) if match and float(match[1]) > 0 else None


def degree_met(text: str, cv_text: str) -> Degree | None:
    if not re.search(
        r"\b(?:degrees?|bachelors?|graduates?|qualifications?|masters?|doctorate|phd)\b", text, re.I
    ):
        return None
    required = degree(text)
    level = required.level if required else 1
    technical = bool(re.search(_TECHNICAL + r"|\btechnical\b", text, re.I))
    # Evaluate each line so a higher nontechnical degree cannot hide a technical bachelor.
    for line in cv_text.splitlines():
        candidate = degree(line)
        if candidate and candidate.level >= level and (not technical or candidate.technical):
            return candidate
    return None
