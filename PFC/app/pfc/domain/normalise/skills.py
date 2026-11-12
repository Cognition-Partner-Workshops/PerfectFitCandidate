"""CT skill tokenisation (REQ-MAT-05, FR-ING-11)."""

import re
from dataclasses import dataclass

from .synonyms import SynonymTable
from .text import normalise_term


@dataclass(frozen=True)
class SkillToken:
    raw: str
    normalised: str
    canonical: str | None


@dataclass(frozen=True)
class TokenisedSkills:
    tokens: tuple[SkillToken, ...]
    empty_count: int


def tokenise_skills(csv_text: str, synonyms: SynonymTable) -> TokenisedSkills:
    """FR-ING-11: retain unknown skills and split ambiguous separators only if known."""
    tokens = []
    seen = set()
    empty_count = 0

    def split_known(text: str) -> list[str]:
        if synonyms.resolve(text):
            return [text]
        for separator in re.finditer(r"/|\s+and\s+", text, re.IGNORECASE):
            left, right = text[: separator.start()].strip(), text[separator.end() :].strip()
            parts = split_known(right)
            if synonyms.resolve(left) and all(synonyms.resolve(part) for part in parts):
                return [left, *parts]
        return [text]

    for chunk in re.split(r"[,;]|\r\n|\r|\n", csv_text):
        if not chunk.strip():
            empty_count += 1
            continue
        for raw in split_known(chunk.strip()):
            norm = normalise_term(raw)
            canonical = synonyms.resolve(raw)
            key = normalise_term(canonical) if canonical else norm
            if key and key not in seen:
                tokens.append(SkillToken(raw, norm, canonical))
                seen.add(key)
    return TokenisedSkills(tuple(tokens), empty_count)
