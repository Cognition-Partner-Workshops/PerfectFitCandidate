"""Text preparation for REQ-IN-03 and REQ-MAT-05 (TDD §6.2)."""

import re
import unicodedata


def normalise_text(s: str) -> str:
    """Apply REQ-IN-03 text repairs while preserving section and bullet boundaries."""
    s = unicodedata.normalize("NFKC", s).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub("[\u200b-\u200d\ufeff]", "", s).replace("\xa0", " ")
    s = re.sub(r"(?m)^\s*```[^\n]*", "", s)
    s = s.replace("**", "").replace("__", "").replace("```", "")
    lines = []
    for line in s.splitlines():
        line = re.sub(r"^\s*[?•▪·\uf0b7*]\s+", "- ", line)
        line = re.sub(r"^\s*#+\s*", "", line)
        line = " ".join(line.split())
        if line:
            lines.append(line)
    return "\n".join(lines)


def normalise_term(s: str) -> str:
    """REQ-MAT-05 matching key; preserve technical skill punctuation."""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub("[\u200b-\u200d\ufeff]", "", s)
    s = "".join(c if c.isalnum() or c.isspace() or c in "+#./" else " " for c in s)
    return " ".join(s.split())
