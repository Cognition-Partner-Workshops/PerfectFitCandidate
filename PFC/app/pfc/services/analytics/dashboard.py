"""Phase-1 dashboard aggregations; dates use the application's clock."""

import json
import math
import re
from collections import Counter
from dataclasses import fields
from datetime import date, datetime
from typing import Any

from pfc.domain.scoring.models import Resource
from pfc.security.problems import Problem
from pfc.services.matching import export
from pfc.services.matching.results import _mask_email

from .population import BANDS, Population, capacity, counts


def percent(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def iso_date(value: Any) -> bool:
    if value is None:
        return True
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def coverage(p: Population) -> float:
    covered = {r["demand_id"] for r in p.results if r["effective_band"] in BANDS[:2]}
    return percent(sum(d["demand_id"] in covered for d in p.demands), len(p.demands))


def emails(p: Population) -> list[dict[str, Any]]:
    demand_ids = {
        row["demand_id"]
        for row in p.db.execute("SELECT * FROM demands")
        if p.demand_filter(dict(row))
    }
    resource_ids = {
        row["resource_id"]
        for row in p.db.execute(
            "SELECT r.*,COALESCE(c.identity_status,'incomplete') AS identity_status,"
            "COALESCE(c.classification_override,c.classification,'Unknown') AS candidate_type "
            "FROM resources r LEFT JOIN candidates c USING(candidate_id)"
        )
        if p.person_filter(dict(row))
    }
    rows = p.db.execute(
        "SELECT e.*,i.started_at AS uploaded_at FROM email_messages e "
        "JOIN demands d USING(demand_id) LEFT JOIN ingestion_runs i "
        "ON i.ingestion_run_id=d.first_ingestion_id"
    )
    return [
        dict(row)
        for row in rows
        if row["demand_id"] in demand_ids and row["resource_id"] in resource_ids
    ]


def within_day(row: dict[str, Any]) -> bool:
    if not row["sent_at"] or not row["uploaded_at"]:
        return False
    try:
        elapsed = (
            datetime.fromisoformat(row["sent_at"]) - datetime.fromisoformat(row["uploaded_at"])
        ).total_seconds()
        return 0 <= elapsed <= 86400
    except (TypeError, ValueError):
        return False


def summary(p: Population) -> dict[str, Any]:
    run = p.run
    dq = json.loads((run or {}).get("dq_summary_json") or "{}")
    assessed = {int(key) for key in dq.get("demands", {})}
    if run is not None:
        assessed.update(
            row[0]
            for row in p.db.execute(
                "SELECT DISTINCT demand_id FROM match_results WHERE run_id=?", (run["run_id"],)
            )
        )
    if (
        p.filters.code_requisition
        or p.filters.account_name
        or p.filters.bu
        or p.filters.grade
        or p.filters.region
    ):
        assessed = {
            row["demand_id"]
            for row in p.db.execute("SELECT * FROM demands")
            if row["demand_id"] in assessed and p.demand_filter(dict(row))
        }
    suitable = {row["demand_id"] for row in p.results if row["effective_band"] in BANDS[:3]}
    issues = {}
    for kind in ("DL", "CT"):
        issues[kind.lower()] = p.db.execute(
            "SELECT COUNT(*) FROM ingestion_row_issues WHERE severity IN ('warning','error') "
            "AND ingestion_run_id=(SELECT ingestion_run_id FROM ingestion_runs "
            "WHERE kind=? AND status='committed' "
            "ORDER BY committed_at DESC,ingestion_run_id DESC LIMIT 1)",
            (kind,),
        ).fetchone()[0]
    approved = [row for row in emails(p) if row["approved_at"] is not None]
    run_view = (
        None
        if run is None
        else {
            key: run[key]
            or (
                0
                if key in {"demands_assessed", "pairs_scored", "roles_without_candidate"}
                else run[key]
            )
            for key in (
                "run_id",
                "status",
                "finished_at",
                "llm_model",
                "template_version",
                "demands_assessed",
                "pairs_scored",
                "roles_without_candidate",
            )
        }
    )
    if run_view is not None:
        run_view["llm_model"] = run_view["llm_model"] or ""
    return {
        "run": run_view,
        "requisitions_assessed": (run or {}).get("demands_assessed") or 0,
        "open_requisitions": len(p.demands),
        "role_to_person_combinations": (run or {}).get("pairs_scored") or 0,
        "roles_with_no_suitable_candidate": len(assessed - suitable),
        "weak_only_requisitions": sum(
            bool(dq.get("demands", {}).get(str(key), {}).get("weak_only")) for key in assessed
        ),
        "data_quality_issues": {**issues, "total": sum(issues.values())},
        "eligible_bench": sum(row["eligibility"] == "eligible" for row in p.resources),
        "excluded_bench": sum(row["eligibility"] == "excluded" for row in p.resources),
        "match_coverage_pct": coverage(p),
        "emails_within24h_pct": percent(sum(within_day(row) for row in approved), len(approved)),
    }


def demand(p: Population) -> dict[str, Any]:
    bu_region = Counter(
        (row["bu"] or "Unspecified", row["region"] or "Unspecified") for row in p.demands
    )
    skills = [
        skill
        for row in p.demands
        for skill in set(row["mandatory_skills"] + row["preferred_skills"])
    ]
    grades = Counter(row["code_local_grade"] for row in p.demands)
    pipeline: Counter[str] = Counter()
    for row in p.demands:
        value = row["position_start_date"]
        if value is not None and iso_date(value):
            pipeline["Overdue" if value < p.today.isoformat() else value[:7]] += 1
    return {
        "open_by_bu_region": [
            {"bu": bu, "region": region, "count": count}
            for (bu, region), count in sorted(bu_region.items())
        ],
        "top_skills": counts(skills, 5),
        "grade_to_jd_count": [{"label": grade, "count": grades[grade]} for grade in p.grades],
        "top_accounts": counts([row["account_name"] or "Not recorded" for row in p.demands], 3),
        "start_pipeline": [
            {"label": label, "count": pipeline[label]}
            for label in sorted(pipeline, key=lambda key: (key != "Overdue", key))
        ],
        "rows": p.demand_rows(),
    }


def bench(p: Population) -> dict[str, Any]:
    eligible = [row for row in p.resources if row["eligibility"] == "eligible"]
    supply = []
    for item in demand(p)["top_skills"]:
        holders = [
            row
            for row in eligible
            if item["label"].casefold() in {s.casefold() for s in row["skills"]}
        ]
        yes = sum(capacity(row["available_capacity"]) == "Y" for row in holders)
        supply.append(
            {
                "skill": item["label"],
                "demand_count": item["count"],
                "supply_y": yes,
                "supply_n": len(holders) - yes,
            }
        )
    timeline = []
    aging = [0] * 5
    for row in eligible:
        start = row["bench_start_date"]
        end = row["bench_end_date"] or p.today.isoformat()
        if start is None or not iso_date(start) or not iso_date(end):
            continue
        days = max(0, (date.fromisoformat(end) - date.fromisoformat(start)).days)
        timeline.append(
            {
                "name": row["display_name"] or row["short_name"],
                "start": start,
                "end": end,
                "days": days,
            }
        )
        aging[
            next(i for i, boundary in enumerate((14, 28, 56, 84, math.inf)) if days < boundary)
        ] += 1
    statuses: dict[str, dict[str, Any]] = {}
    for row in p.resources:
        key = row["bench_status"] or "Not recorded"
        if key not in statuses:
            statuses[key] = {
                "status": key,
                "count": 0,
                "excluded": row["status_excluded"],
                "priority": row["status_priority"] or 999,
            }
        statuses[key]["count"] += 1
    ordered = sorted(
        statuses.values(), key=lambda row: (row["excluded"], row["priority"], row["status"])
    )
    return {
        "top_skills": counts([s for row in eligible for s in set(row["skills"])], 5),
        "demand_vs_supply": supply,
        "grade_histogram": [
            {
                "grade": grade,
                "bench": sum(row["grade"] == grade for row in eligible),
                "demand": sum(row["code_local_grade"] == grade for row in p.demands),
            }
            for grade in p.grades
        ],
        "timeline": timeline,
        "aging": [
            {"label": label, "count": count}
            for label, count in zip(
                ("0–2 wks", "2–4 wks", "4–8 wks", "8–12 wks", "> 12 wks"), aging, strict=True
            )
        ],
        "status_distribution": [
            {key: row[key] for key in ("status", "count", "excluded")} for row in ordered
        ],
        "rows": p.resource_rows(),
    }


def validation(p: Population) -> list[dict[str, Any]]:
    """REQ-OUT-01/02/03/10, P1-EX-04: inspect scored facts and output serializers."""
    ends, distances = [], []
    capacity_ok = all("capacity" not in field.name.casefold() for field in fields(Resource))
    email_ok, masked_ok, score_ok, dates_ok = True, True, True, True
    headers = export.HEADERS
    layout_ok = (
        "Potential Person" in headers
        and "Person Email" in headers
        and headers.index("Person Email") == headers.index("Potential Person") + 1
    )
    for row in p.results:
        inputs = {
            record["component"]: json.loads(record["inputs_json"])
            for record in p.db.execute(
                "SELECT component,inputs_json FROM score_breakdowns WHERE result_id=?",
                (row["result_id"],),
            )
        }
        status = inputs.get("bench_end", {}).get("parse_status")
        flagged = "Bench End Date unparseable — pending review" in row["gaps_checks"]
        ends.append(status if status != "unparseable" or flagged else "unflagged")
        distances.append(inputs.get("grade", {}).get("distance"))
        capacity_ok = capacity_ok and not _capacity_input(inputs)
        source = p.db.execute(
            "SELECT email_address FROM resources WHERE resource_id=?", (row["resource_id"],)
        ).fetchone()[0]
        snapshot = inputs.get("skills", {}).get("display_facts", {}).get("person_email")
        expected = _mask_email(source)
        email_ok = (
            email_ok
            and bool(source)
            and (
                snapshot == expected
                if "person_email" in inputs.get("skills", {}).get("display_facts", {})
                else row["person_email"] == expected
            )
        )
        masked_ok = masked_ok and bool(source) and row["person_email"] == _required_mask(source)
        score = row["score"]
        score_ok = score_ok and (
            isinstance(score, (int, float))
            and math.isfinite(score)
            and 0 <= score <= 100
            and re.fullmatch(r"(?:100\.0|\d{1,2}\.\d)%", export.display_percentage(score))
            is not None
        )
        try:
            displayed = export.display_start_date(row["start_date"])
            dates_ok = (
                dates_ok
                and iso_date(row["start_date"])
                and (displayed == "" or iso_date(displayed))
            )
        except (Problem, TypeError, ValueError):
            dates_ok = False
    pending = ends.count("unparseable")
    checks = [
        (
            "bench-end",
            "Bench End is blank or unparseable with pending review",
            all(value in {"blank", "unparseable"} for value in ends),
            f"{pending} pending-review unparseable end dates; scored inputs checked.",
        ),
        (
            "capacity",
            "Available Capacity is not a matching/filter input",
            capacity_ok,
            "Structural Resource fields and persisted score-breakdown input keys checked.",
        ),
        (
            "grade",
            "Every included person is within one grade",
            all(_distance_ok(value) for value in distances),
            f"Persisted grade distances checked for {len(distances)} results.",
        ),
        (
            "email-copy",
            "Result email agrees with the CT source (masked snapshot)",
            email_ok,
            "Masked scoring snapshot compared exactly with the mask of resources.email_address; "
            "legacy rows use live CT serialization. Raw historical email bytes are not persisted, "
            "so equality of hidden characters cannot be proven.",
        ),
        (
            "masked-email",
            "Masked email immediately follows the person's name",
            layout_ok and masked_ok,
            "Serialized export column order and REQ-OUT-03 mask checked against CT source.",
        ),
        (
            "evidence",
            "Every result has evidence and gaps/checks",
            all(
                bool(row["matching_evidence"].strip() and row["gaps_checks"].strip())
                for row in p.results
            ),
            "Persisted matching evidence and gaps/checks checked.",
        ),
        (
            "score",
            "Match percentage is percentage-formatted and within 0–100",
            score_ok,
            "Persisted scores and the CSV serializer's formatted percentage checked.",
        ),
        (
            "start-date",
            "Start Date is ISO or null",
            dates_ok,
            "Scored Start Dates and ISO serializer checked (current values for legacy rows).",
        ),
    ]
    return [
        {"id": key, "label": label, "passed": passed, "detail": detail}
        for key, label, passed, detail in checks
    ]


def _required_mask(value: str) -> str:
    """REQ-OUT-03: independently verify six masked positions after the first two."""
    local, separator, domain = value.partition("@")
    return (
        (
            "*" * len(local)
            if len(local) <= 2
            else local[:2] + "*" * min(6, len(local) - 2) + local[8:]
        )
        + separator
        + domain
    )


def _capacity_input(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            "capacity" in re.sub(r"[^a-z]", "", str(key).casefold()) or _capacity_input(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(_capacity_input(item) for item in value)


def _distance_ok(value: Any) -> bool:
    try:
        return 0 <= float(value) <= 1
    except (TypeError, ValueError):
        return False


def match(p: Population) -> dict[str, Any]:
    return {
        "coverage_pct": coverage(p),
        "band_distribution": []
        if p.run is None
        else [
            {"label": band, "count": sum(row["effective_band"] == band for row in p.results)}
            for band in BANDS
        ],
        "email_status_counts": []
        if p.run is None
        else counts([row["status"] for row in emails(p)]),
        "validation": [] if p.run is None else validation(p),
    }
