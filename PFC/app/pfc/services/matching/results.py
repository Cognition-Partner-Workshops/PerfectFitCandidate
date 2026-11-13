"""Masked results and non-destructive overrides (REQ-OUT-01/03, REQ-MAT-09)."""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pfc.services.pii import mask_email, mask_emails_in_json

from .common import atomic, audit, timestamp
from .current import run_pair_predicate
from .data_changes import annotate_data_changes
from .lifecycle import reconcile_runs

BANDS = {"Strong", "Potential", "Avg", "Weak", "Insufficient data"}
ACTIONS = {"pin", "unpin", "exclude", "include", "adjust_band"}
FilterValue = str | list[str] | None


def filter_values(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    return [item for item in values if item != ""]


class ResultFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    requisition: FilterValue = None
    account: FilterValue = None
    demand_grade: FilterValue = None
    person_grade: FilterValue = None
    person_location: FilterValue = None
    band: FilterValue = None
    location_assessment: str | None = None
    mandatory_skill: str | None = None
    score_min: float | None = Field(default=None, ge=0, le=100)
    score_max: float | None = Field(default=None, ge=0, le=100)
    status: FilterValue = None
    bu: FilterValue = None
    region: FilterValue = None
    candidate_type: FilterValue = None
    incomplete_identity: Literal["yes", "no", ""] | list[Literal["yes", "no", ""]] | None = None
    mandatory_skills: Literal["met", "gap", ""] | list[Literal["met", "gap", ""]] | None = None
    shortlisted_only: bool = False

    @model_validator(mode="after")
    def limits(self) -> Self:
        if (
            self.score_min is not None
            and self.score_max is not None
            and self.score_min > self.score_max
        ):
            raise ValueError("score_min exceeds score_max")
        if any(value not in BANDS | {"Weak Match"} for value in filter_values(self.band)):
            raise ValueError("Unknown match band")
        return self


def _mask_email(value: str | None) -> str | None:
    return mask_email(value)


def list_runs(db: sqlite3.Connection, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    _paging(limit, offset)
    reconcile_runs(db)
    runs = [
        dict(row)
        for row in db.execute(
            "SELECT r.*,t.version AS template_version FROM match_runs r "
            "JOIN scoring_templates t USING(template_id) ORDER BY run_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    ]
    annotate_data_changes(db, runs)
    return runs


def get_run(db: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    reconcile_runs(db, run_id)
    row = db.execute(
        "SELECT r.*,t.version AS template_version FROM match_runs r "
        "JOIN scoring_templates t USING(template_id) WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    run = dict(row)
    annotate_data_changes(db, [run])
    return run


def _paging(limit: int | None, offset: int) -> None:
    if (limit is not None and not 1 <= limit <= 5000) or offset < 0:
        raise ValueError("limit must be 1..5000 and offset nonnegative")


def active_overrides(db: sqlite3.Connection, run_id: int) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in db.execute(
        "SELECT o.* FROM overrides o JOIN match_results m USING(result_id) "
        "WHERE m.run_id=? AND o.revoked_at IS NULL ORDER BY o.override_id",
        (run_id,),
    ):
        grouped[row["result_id"]].append(dict(row))
    return grouped


def active_overrides_for_pairs(
    db: sqlite3.Connection, run_pairs: dict[int, int]
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    predicate, parameters = run_pair_predicate(run_pairs)
    for row in db.execute(
        "SELECT o.* FROM overrides o JOIN match_results m USING(result_id) "
        f"WHERE o.revoked_at IS NULL AND {predicate} ORDER BY o.override_id",
        parameters,
    ):
        grouped[row["result_id"]].append(dict(row))
    return grouped


def apply_overrides(row: dict[str, Any], overrides: list[dict[str, Any]]) -> None:
    row.update(
        overridden=bool(overrides),
        effective_band=row["band"],
        effective_shortlisted=bool(row.get("shortlisted")),
        effective_excluded=False,
    )
    for override in overrides:
        action = override["action"]
        if action == "adjust_band":
            row["effective_band"] = override["new_band"]
        elif action in {"pin", "include", "unpin"}:
            row["effective_shortlisted"] = action != "unpin"
            if action == "include":
                row["effective_excluded"] = False
        elif action == "exclude":
            row["effective_excluded"] = True
    if row["effective_excluded"]:
        row["effective_shortlisted"] = False
    row["effective_email_preselected"] = (
        bool(row.get("email_preselected")) and row["effective_shortlisted"]
    )
    row["display_band"] = "Weak Match" if row["effective_band"] == "Weak" else row["effective_band"]


def _rows(
    db: sqlite3.Connection,
    run_id: int | None = None,
    *,
    run_pairs: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    parameters: tuple[int, ...]
    if run_pairs is None:
        if run_id is None:
            return []
        predicate, parameters = "m.run_id=?", (run_id,)
        overrides_by_result = active_overrides(db, run_id)
    else:
        if not run_pairs:
            return []
        predicate, parameters = run_pair_predicate(run_pairs)
        overrides_by_result = active_overrides_for_pairs(db, run_pairs)
    rows = db.execute(
        "SELECT m.*,d.code_requisition AS requisition,d.position_start_date AS start_date,"
        "d.account_name AS account,d.demand_request_name AS role,d.bu,"
        "d.code_local_grade AS demand_grade,"
        "COALESCE(NULLIF(d.region,''),'Unspecified') AS role_location,"
        "r.short_name AS potential_person,r.email_address AS person_email,r.grade AS person_grade,"
        "r.location AS person_location,r.bench_status AS status,b.inputs_json AS scored_inputs,"
        "COALESCE(c.classification_override,c.classification,'Unknown') AS candidate_type,"
        "COALESCE(c.identity_status!='complete',0) AS incomplete_identity "
        "FROM match_results m JOIN demands d USING(demand_id) JOIN resources r USING(resource_id) "
        "LEFT JOIN candidates c ON c.candidate_id=m.candidate_id "
        "LEFT JOIN score_breakdowns b ON b.result_id=m.result_id AND b.component='skills' "
        f"WHERE {predicate} ORDER BY m.demand_id,m.rank,m.result_id",
        parameters,
    ).fetchall()
    result = []
    for source in rows:
        row = dict(source)
        scored = json.loads(row.pop("scored_inputs") or "{}")
        row.update(scored.get("display_facts", {}))
        row["person_email"] = _mask_email(row["person_email"])
        apply_overrides(row, overrides_by_result.get(row["result_id"], []))
        result.append(row)
    return result


def list_results(
    db: sqlite3.Connection,
    run_id: int | None = None,
    filters: ResultFilters | dict[str, Any] | None = None,
    *,
    sort: str = "rank",
    descending: bool = False,
    limit: int | None = 50,
    offset: int = 0,
    run_pairs: dict[int, int] | None = None,
) -> dict[str, Any]:
    """REQ-OUT-01/03: sixteen display fields, masked before leaving the service."""
    _paging(limit, offset)
    allowed = {
        "rank",
        "score",
        "requisition",
        "account",
        "demand_grade",
        "person_grade",
        "person_location",
        "potential_person",
        "status_priority",
        "band",
        "start_date",
    }
    if sort not in allowed:
        raise ValueError("Unsupported result sort")
    f = (
        filters
        if isinstance(filters, ResultFilters)
        else ResultFilters.model_validate(filters or {})
    )
    if run_pairs is not None and run_id is not None:
        raise ValueError("run_id and run_pairs are mutually exclusive")
    if run_pairs is None and run_id is None:
        reconcile_runs(db)
        latest = db.execute(
            "SELECT run_id FROM match_runs WHERE status='completed' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        run_id = latest[0] if latest else None
    if run_pairs is None and run_id is None:
        return {"run_id": None, "items": [], "total": 0, "limit": limit, "offset": offset}
    output: list[dict[str, Any]] = []
    for row in _rows(db, run_id, run_pairs=run_pairs):
        if f.shortlisted_only and not row["effective_shortlisted"]:
            continue
        requisitions = filter_values(f.requisition)
        if requisitions and str(row["requisition"] or "").casefold() not in {
            value.casefold() for value in requisitions
        }:
            continue
        if any(
            filter_values(getattr(f, key)) and row[key] not in filter_values(getattr(f, key))
            for key in ("demand_grade", "person_grade")
        ):
            continue
        if (
            f.location_assessment is not None
            and row["location_assessment"] != f.location_assessment
        ):
            continue
        if any(
            filter_values(getattr(f, key))
            and str(row.get(column) or "").casefold()
            not in {value.casefold() for value in filter_values(getattr(f, key))}
            for key, column in (
                ("account", "account"),
                ("person_location", "person_location"),
                ("status", "status"),
                ("bu", "bu"),
                ("region", "role_location"),
                ("candidate_type", "candidate_type"),
            )
        ):
            continue
        incomplete = filter_values(f.incomplete_identity)
        if incomplete and ("yes" if row["incomplete_identity"] else "no") not in incomplete:
            continue
        mandatory_skills = filter_values(f.mandatory_skills)
        if (
            mandatory_skills
            and ("met" if row["mandatory_missing"] == 0 else "gap") not in mandatory_skills
        ):
            continue
        bands = filter_values(f.band)
        if bands and not set(bands) & {row["effective_band"], row["display_band"]}:
            continue
        if f.score_min is not None and row["score"] < f.score_min:
            continue
        if f.score_max is not None and row["score"] > f.score_max:
            continue
        if f.mandatory_skill:
            skills = db.execute(
                "SELECT i.text,s.name FROM jd_requirement_items i "
                "LEFT JOIN skills s ON s.skill_id=i.canonical_skill_id "
                "WHERE i.jd_req_id=? AND i.kind='mandatory_skill'",
                (row["jd_req_id"],),
            ).fetchall()
            if not any(
                f.mandatory_skill.casefold() in str(value or "").casefold()
                for item in skills
                for value in item
            ):
                continue
        output.append(row)

    # Input order supplies deterministic demand/rank/result tie-breaks for every sort.
    def key(row: dict[str, Any]) -> tuple[bool, Any]:
        value = row["effective_band"] if sort == "band" else row[sort]
        return value is None, value.casefold() if isinstance(value, str) else value

    if sort == "score":
        if run_pairs is not None:
            output.sort(
                key=lambda row: (
                    str(row["requisition"] or "").casefold(),
                    row["rank"],
                    row["result_id"],
                )
            )
        output.sort(key=key, reverse=descending)
    elif not (run_pairs is not None and sort == "rank" and not descending):
        output.sort(key=key, reverse=descending)
    return {
        "run_id": run_id,
        "items": output[offset : offset + limit if limit is not None else None],
        "total": len(output),
        "limit": limit,
        "offset": offset,
    }


def demand_matches(db: sqlite3.Connection, demand_id: int) -> dict[str, Any]:
    reconcile_runs(db)
    for row in db.execute(
        "SELECT run_id,dq_summary_json FROM match_runs WHERE status='completed' "
        "ORDER BY run_id DESC"
    ):
        summary = json.loads(row["dq_summary_json"] or "{}").get("demands", {})
        if str(demand_id) in summary:
            return {
                "run_id": row["run_id"],
                **summary[str(demand_id)],
                "items": [
                    item for item in _rows(db, row["run_id"]) if item["demand_id"] == demand_id
                ],
            }
    return {
        "run_id": None,
        "items": [],
        "no_suitable_candidate": True,
        "weak_only": False,
        "flags": [],
    }


def result_detail(db: sqlite3.Connection, result_id: int) -> dict[str, Any] | None:
    run = db.execute("SELECT run_id FROM match_results WHERE result_id=?", (result_id,)).fetchone()
    if run is None:
        return None
    result = next(row for row in _rows(db, run[0]) if row["result_id"] == result_id)
    result["breakdown"] = [
        {**dict(row), "inputs": mask_emails_in_json(json.loads(row["inputs_json"]))}
        for row in db.execute(
            "SELECT * FROM score_breakdowns WHERE result_id=? ORDER BY CASE component "
            "WHEN 'skills' THEN 0 WHEN 'grade' THEN 1 WHEN 'location' THEN 2 "
            "WHEN 'bench_start' THEN 3 WHEN 'bench_end' THEN 4 ELSE 5 END",
            (result_id,),
        )
    ]
    result["cv_scores"] = [
        dict(row)
        for row in db.execute(
            "SELECT m.*,v.version_no,v.is_latest FROM match_cv_scores m LEFT JOIN cv_versions v "
            "USING(cv_version_id) WHERE m.result_id=? ORDER BY m.cv_version_id",
            (result_id,),
        )
    ]
    result["overrides"] = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM overrides WHERE result_id=? ORDER BY override_id",
            (result_id,),
        )
    ]
    return result


def excluded_view(db: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT e.*,r.short_name,d.code_requisition AS requisition "
            "FROM eligibility_exclusions e "
            "JOIN resources r USING(resource_id) LEFT JOIN demands d USING(demand_id) "
            "WHERE e.run_id=? ORDER BY r.short_name COLLATE NOCASE,e.resource_id,"
            "e.demand_id,e.reason_code",
            (run_id,),
        )
    ]


def excluded_current_view(
    db: sqlite3.Connection,
    run_pairs: dict[int, int],
    requisitions: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not run_pairs:
        return []
    demands = {
        int(row["demand_id"]): str(row["code_requisition"] or "")
        for row in db.execute(
            "SELECT demand_id,code_requisition FROM demands WHERE lifecycle='open'"
        )
    }
    requested = {value.casefold() for value in filter_values(requisitions)}
    grouped: dict[int, set[int]] = defaultdict(set)
    for demand_id, run_id in run_pairs.items():
        if requested and demands.get(demand_id, "").casefold() not in requested:
            continue
        grouped[run_id].add(demand_id)

    newest_run_id = max(run_pairs.values())
    rows = []
    for run_id in sorted(set(run_pairs.values()), reverse=True):
        for row in excluded_view(db, run_id):
            if row["demand_id"] is None:
                if run_id == newest_run_id:
                    rows.append(row)
            elif row["demand_id"] in grouped.get(run_id, set()):
                rows.append(row)
    return rows


def _reason(reason: str) -> str:
    if not reason.strip():
        raise ValueError("Override reason is required")
    return reason


def add_override(
    db: sqlite3.Connection,
    result_id: int,
    action: Literal["pin", "unpin", "exclude", "include", "adjust_band"],
    new_band: str | None,
    reason: str,
    user_id: int,
    now: datetime,
) -> int:
    _reason(reason)
    if action not in ACTIONS or (action == "adjust_band" and new_band not in BANDS):
        raise ValueError("Invalid override action or band")
    if action != "adjust_band" and new_band is not None:
        raise ValueError("new_band is only valid for adjust_band")
    with atomic(db):
        row = db.execute("SELECT * FROM match_results WHERE result_id=?", (result_id,)).fetchone()
        if row is None:
            raise ValueError("Match result not found")
        cursor = db.execute(
            "INSERT INTO overrides(demand_id,resource_id,result_id,action,new_band,reason,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                row["demand_id"],
                row["resource_id"],
                result_id,
                action,
                new_band,
                reason,
                user_id,
                timestamp(now),
            ),
        )
        override_id = int(cursor.lastrowid or 0)
        audit(
            db,
            "match.override",
            "overrides",
            override_id,
            user_id,
            now,
            result_id=result_id,
            override_action=action,
            reason=reason,
            new_band=new_band,
        )
    return override_id


def revoke_override(
    db: sqlite3.Connection,
    override_id: int,
    reason: str,
    user_id: int,
    now: datetime,
) -> None:
    _reason(reason)
    with atomic(db):
        cursor = db.execute(
            "UPDATE overrides SET revoked_by=?,revoked_at=? "
            "WHERE override_id=? AND revoked_at IS NULL",
            (user_id, timestamp(now), override_id),
        )
        if not cursor.rowcount:
            raise ValueError("Active override not found")
        audit(db, "match.override.revoke", "overrides", override_id, user_id, now, reason=reason)
