"""Approved synonym matching and non-matching suggestions (REQ-MAT-05)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .text import normalise_term


@dataclass(frozen=True)
class SynonymRow:
    canonical: str
    term: str
    relation: str = "equivalent"
    credit: float = 1.0
    status: str = "approved"

    def __post_init__(self) -> None:
        if self.relation not in {"equivalent", "abbreviation", "misspelling", "related"}:
            raise ValueError("Unknown synonym relation")
        if self.status not in {"suggested", "approved", "rejected"}:
            raise ValueError("Unknown synonym status")
        if not 0 <= self.credit <= 1:
            raise ValueError("Synonym credit must be between 0 and 1")
        if self.relation != "related" and self.credit != 1:
            raise ValueError("Equivalent synonyms require full credit")
        if not normalise_term(self.canonical) or not normalise_term(self.term):
            raise ValueError("Synonym canonical and term must not be blank")


class SynonymTable:
    """REQ-MAT-05: approved rows only; fuzzy suggestions never resolve automatically."""

    def __init__(self, rows: Iterable[SynonymRow | Mapping[str, object]]) -> None:
        parsed = []
        for row in rows:
            if isinstance(row, SynonymRow):
                parsed.append(row)
            else:
                parsed.append(
                    SynonymRow(
                        canonical=str(row["canonical"]),
                        term=str(row["term"]),
                        relation=str(row.get("relation", "equivalent")),
                        credit=float(str(row.get("credit", 1))),
                        status=str(row.get("status", "approved")),
                    )
                )
        self.rows = tuple(parsed)
        self._exact: dict[str, str] = {}
        self._related: dict[str, list[tuple[str, float]]] = {}
        self._terms: dict[str, set[str]] = {}
        for row in self.rows:
            if row.status == "approved":
                key = normalise_term(row.canonical)
                self._exact[key] = row.canonical
                self._terms.setdefault(key, set()).add(key)
        for row in self.rows:
            if row.status != "approved":
                continue
            term = normalise_term(row.term)
            canonical = normalise_term(row.canonical)
            if row.relation == "related":
                self._related.setdefault(term, []).append((row.canonical, row.credit))
            else:
                if term in self._exact and normalise_term(self._exact[term]) != canonical:
                    raise ValueError(f"Ambiguous equivalent synonym: {row.term}")
                self._exact[term] = self._exact[canonical]
                self._terms[canonical].add(term)

    def resolve(self, term: str) -> str | None:
        return self._exact.get(normalise_term(term))

    def related(self, term: str) -> list[tuple[str, float]]:
        return list(self._related.get(normalise_term(term), []))

    def expand(self, term: str) -> set[str]:
        canonical = self.resolve(term)
        return set(self._terms[normalise_term(canonical)]) if canonical else set()

    def suggest(self, term: str) -> list[str]:
        key = normalise_term(term)
        return sorted(
            {
                name
                for name in self._exact.values()
                if _edit_distance(key, normalise_term(name)) <= 2
            }
        )


def _edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]
