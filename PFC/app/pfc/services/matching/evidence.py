"""REQ-MAT-06: presentation-only checks over frozen, grounded experience facts."""

import json
import re
import sqlite3
from typing import Any

from pfc.domain.normalise import normalise_term
from pfc.domain.scoring import Resource
from pfc.services.ingestion.cv_facts import validate_facts

DURATION_GAP = "Experience duration not recorded — requires validation"
NEGATED = re.compile(r"\b(no|not|without|under|less than|up to)\b", re.I)


def shows_duration(value: str, evidence: str, text: str, years: float) -> bool:
    if NEGATED.search(evidence):
        return False
    term = re.sub(
        r"\b(?:\d+(?:\.\d+)?|minimum|at|least|years?|yrs?|of|in|experience)\b",
        " ",
        normalise_term(text),
    )
    term = " ".join(term.split())
    amount = r"(?P<years>\d+(?:\.\d+)?)\+?\s*(?:years?|yrs?)\b"
    patterns: tuple[str, ...]
    if term:
        specialist = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        patterns = (
            amount + r"\s+(?:of\s+)?" + specialist,
            specialist + r"(?:\s+experience)?\s*[:—–-]?\s*(?:for\s+)?" + amount,
        )
    else:
        patterns = (amount,)
    for candidate in (value, evidence):
        if NEGATED.search(candidate):
            continue
        candidate = candidate.casefold()
        for pattern in patterns:
            for match in re.finditer(pattern, candidate):
                prefix = candidate[: match.start("years")]
                if re.search(r"[\d.]\s*(?:[-—–]|to)\s*$", prefix):
                    continue
                if float(match["years"]) >= years:
                    return True
    return False


def snapshot_experience(
    db: sqlite3.Connection, metadata: dict[str, dict[str, Any]], resources: list[Resource]
) -> dict[str, Any]:
    requirements = {}
    for meta in metadata.values():
        req_id = meta["jd_req_id"]
        data = json.loads(
            db.execute(
                "SELECT extraction_json FROM jd_requirements WHERE jd_req_id=?",
                (req_id,),
            ).fetchone()[0]
            or "{}"
        )
        items = db.execute(
            "SELECT item_id,text,position FROM jd_requirement_items WHERE jd_req_id=? "
            "AND kind='experience' ORDER BY position,item_id",
            (req_id,),
        ).fetchall()
        offset = sum(len(data.get(field, [])) for field in ("mandatory_skills", "preferred_skills"))
        experience = data.get("experience", [])
        timed = []
        for item in items:
            if not experience:
                continue
            index = item["position"] - offset
            if not 0 <= index < len(experience) or experience[index]["text"] != item["text"]:
                raise ValueError("Experience item does not match its confirmed extraction")
            detail = experience[index]
            if detail.get("years") is not None:
                timed.append(
                    {
                        "item_id": str(item["item_id"]),
                        "text": item["text"],
                        "years": detail["years"],
                    }
                )
        if timed:
            requirements[str(req_id)] = timed
    if not requirements:
        return {"requirements": {}, "facts": {}}
    candidates: dict[str, list[dict[str, str]]] = {}
    for row in db.execute(
        "SELECT candidate_id,text_norm,facts_json FROM cv_versions WHERE candidate_id IS NOT NULL"
    ):
        raw = json.loads(row["facts_json"] or "{}")
        if not isinstance(raw, dict):
            continue
        facts = raw.get("facts", {})
        if not isinstance(facts, dict):
            continue
        grounded = validate_facts(row["text_norm"], facts)
        candidates.setdefault(f"candidate:{row['candidate_id']}", []).extend(
            {"value": fact.value, "evidence": fact.evidence} for fact in grounded.experience
        )
    return {
        "requirements": requirements,
        "facts": {r.resource_id: candidates.get(r.candidate_id or "", []) for r in resources},
    }


def apply_experience_checks(db: sqlite3.Connection, run_id: int, snapshot: dict[str, Any]) -> None:
    requirements = [key for key, items in snapshot["requirements"].items() if items]
    if not requirements:
        return
    for row in db.execute(
        "SELECT r.result_id,r.resource_id,r.jd_req_id,r.gaps_checks,b.inputs_json "
        "FROM match_results r JOIN score_breakdowns b ON b.result_id=r.result_id "
        "AND b.component='skills' WHERE r.run_id=? "
        "AND r.jd_req_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))",
        (run_id, json.dumps(requirements)),
    ).fetchall():
        facts = snapshot["facts"].get(str(row["resource_id"]), [])
        missing = {
            item["item_id"]: item
            for item in snapshot["requirements"].get(str(row["jd_req_id"]), [])
            if not any(
                shows_duration(fact["value"], fact["evidence"], item["text"], item["years"])
                for fact in facts
            )
        }
        items = json.loads(row["inputs_json"]).get("items", [])
        if not items:
            continue
        evidence = "; ".join(
            f"{missing[item['item_id']]['text']}: {DURATION_GAP}"
            if item["item_id"] in missing
            else item["evidence"]
            for item in items
        )
        gaps = row["gaps_checks"]
        if gaps == DURATION_GAP:
            gaps = ""
        else:
            gaps = gaps.removesuffix("; " + DURATION_GAP)
        if missing:
            gaps = "; ".join(filter(None, (gaps, DURATION_GAP)))
        db.execute(
            "UPDATE match_results SET matching_evidence=?,gaps_checks=? WHERE result_id=?",
            (evidence, gaps, row["result_id"]),
        )
