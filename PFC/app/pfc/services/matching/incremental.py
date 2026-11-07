"""Fingerprint proofs, clean-row carry-forward and shared post-score selection."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from pfc.domain.config_loader import DomainConfig, ScoringTemplate
from pfc.domain.scoring import Demand, Resource, SkillBasis, build_combined_basis
from pfc.domain.scoring.selection import RankingInput, input_key, select_scored_rows

from .common import digest, dumps

DEMAND_FIELDS = (
    "demand_id",
    "current_version_id",
    "jd_req_id",
    "jd_text_norm",
    "extracted_by",
    "position_start_date",
    "account_name",
    "bu",
    "demand_request_name",
    "region",
)


def fingerprints(
    demands: list[Demand],
    resources: list[Resource],
    cvs: dict[str, list[SkillBasis]],
    metadata: dict[str, dict[str, Any]],
    emails: dict[str, str | None],
    config: DomainConfig,
) -> dict[str, Any]:
    return {
        "version": 1,
        "demands": {
            str(metadata[d.code_requisition]["demand_id"]): digest(
                {
                    "demand": repr(d),
                    "metadata": {key: metadata[d.code_requisition][key] for key in DEMAND_FIELDS},
                }
            )
            for d in demands
        },
        "resources": {
            r.resource_id: digest(
                {
                    "resource": repr(r),
                    "email": emails[r.resource_id],
                    "cvs": [asdict(b) for b in cvs.get(r.candidate_id or "", [])],
                    "basis": asdict(
                        build_combined_basis(r, cvs.get(r.candidate_id or "", []), config)
                    ),
                }
            )
            for r in resources
        },
    }


@dataclass(frozen=True)
class Plan:
    mode: str
    reason: str
    base_run_id: int | None
    demands: frozenset[str]
    resources: frozenset[str]

    def provenance(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "base_run_id": self.base_run_id,
            "dirty_demands": len(self.demands),
            "dirty_resources": len(self.resources),
        }


def plan_run(
    requested: bool,
    scope: str,
    config_key: str,
    inputs: dict[str, Any],
    base: dict[str, Any] | None,
) -> Plan:
    def full(reason: str) -> Plan:
        return Plan(
            "full", reason, None, frozenset(inputs["demands"]), frozenset(inputs["resources"])
        )

    if not requested:
        return full("requested_full")
    if scope != "full":
        return full("scope_not_full")
    if base is None:
        return full("no_base_run")
    if base["scope"] != "full":
        return full("base_scope_not_full")
    try:
        old = json.loads(base.get("input_fingerprints_json") or "null")
        if not isinstance(old, dict) or old.get("version") != 1:
            return full("fingerprints_missing")
        for kind in ("demands", "resources"):
            if not isinstance(old.get(kind), dict) or any(
                not isinstance(k, str) or not isinstance(v, str) or len(v) != 64
                for k, v in old[kind].items()
            ):
                return full("fingerprints_invalid")
    except (TypeError, ValueError):
        return full("fingerprints_invalid")
    if base.get("config_key") != config_key:
        return full("config_changed")
    if old["resources"].keys() - inputs["resources"].keys():
        return full("resource_removed")
    if old["demands"].keys() - inputs["demands"].keys():
        return full("demand_removed")
    dirty = {
        kind: frozenset(k for k, value in inputs[kind].items() if old[kind].get(k) != value)
        for kind in ("demands", "resources")
    }
    return Plan(
        "incremental", "inputs_compared", base["run_id"], dirty["demands"], dirty["resources"]
    )


def carry_forward(db: sqlite3.Connection, run_id: int, plan: Plan) -> None:
    for kind, ids in (("demands", plan.demands), ("resources", plan.resources)):
        db.execute(f"CREATE TEMP TABLE dirty_{kind}(id INTEGER PRIMARY KEY)")
        db.executemany(f"INSERT INTO dirty_{kind} VALUES (?)", [(int(i),) for i in ids])
    clean = (
        "NOT EXISTS (SELECT 1 FROM dirty_demands WHERE id=b.demand_id) AND "
        "NOT EXISTS (SELECT 1 FROM dirty_resources WHERE id=b.resource_id)"
    )
    columns = [
        r["name"]
        for r in db.execute("PRAGMA table_info(match_results)")
        if r["name"] not in {"run_id", "result_id"}
    ]
    db.execute(
        f"INSERT INTO match_results(run_id,{','.join(columns)}) "
        f"SELECT ?,{','.join('b.' + c for c in columns)} FROM match_results b "
        f"WHERE b.run_id=? AND {clean}",
        (run_id, plan.base_run_id),
    )
    for table, fields in (
        ("match_cv_scores", "cv_version_id,score,band"),
        ("score_breakdowns", "component,points,max_points,inputs_json"),
    ):
        db.execute(
            f"INSERT INTO {table}(result_id,{fields}) SELECT n.result_id,"
            f"{','.join('s.' + c for c in fields.split(','))} FROM {table} s "
            "JOIN match_results b ON b.result_id=s.result_id "
            "JOIN match_results n ON n.demand_id=b.demand_id AND n.resource_id=b.resource_id "
            "WHERE b.run_id=? AND n.run_id=?",
            (plan.base_run_id, run_id),
        )
    db.execute(
        "INSERT INTO eligibility_exclusions SELECT ?,b.resource_id,b.demand_id,"
        "b.reason_code,b.detail FROM eligibility_exclusions b WHERE b.run_id=? AND "
        "NOT EXISTS (SELECT 1 FROM dirty_resources WHERE id=b.resource_id) AND "
        "(b.demand_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM dirty_demands WHERE id=b.demand_id))",
        (run_id, plan.base_run_id),
    )


def rerank(
    db: sqlite3.Connection,
    run_id: int,
    demands: list[Demand],
    resources: list[Resource],
    metadata: dict[str, dict[str, Any]],
    config: DomainConfig,
    template: ScoringTemplate,
    skipped: list[int],
) -> None:
    people = {r.resource_id: r for r in resources}
    summary: dict[str, Any] = {"skipped_demand_ids": skipped, "demands": {}}
    without = 0
    for demand in demands:
        demand_id = metadata[demand.code_requisition]["demand_id"]
        projections = []
        for row in db.execute(
            "SELECT r.*,b.inputs_json FROM match_results r JOIN score_breakdowns b "
            "ON b.result_id=r.result_id AND b.component='skills' "
            "WHERE r.run_id=? AND r.demand_id=?",
            (run_id, demand_id),
        ):
            inputs = json.loads(row["inputs_json"])
            person = people[str(row["resource_id"])]
            projections.append(
                RankingInput(
                    person,
                    row["score"],
                    row["band"],
                    row["status_priority"],
                    config.grades.distance(person.grade or "", demand.grade or ""),
                    row["mandatory_missing"],
                    float(inputs["semantic"]),
                    any(i["kind"] == "mandatory" and i["credit"] > 0 for i in inputs["items"]),
                )
            )
        selected = select_scored_rows(projections, template)
        shortlist = {r.resource.resource_id for r in selected.shortlist}
        emails = {r.resource.resource_id for r in selected.emails}
        # Full persistence expands duplicate candidates, retaining every scored pair.
        ordered = sorted(projections, key=lambda r: (r.band == "Insufficient data", input_key(r)))
        db.executemany(
            "UPDATE match_results SET rank=?,shortlisted=?,email_preselected=? "
            "WHERE run_id=? AND demand_id=? AND resource_id=?",
            [
                (
                    rank,
                    r.resource.resource_id in shortlist,
                    r.resource.resource_id in emails,
                    run_id,
                    demand_id,
                    int(r.resource.resource_id),
                )
                for rank, r in enumerate(ordered, 1)
            ],
        )
        flags = []
        try:
            config.grades.ordinal(demand.grade or "")
        except ValueError:
            flags.append("Unknown demand grade — demand not scorable")
        summary["demands"][str(demand_id)] = {
            "no_suitable_candidate": selected.no_suitable,
            "weak_only": selected.weak_only,
            "flags": flags,
        }
        without += selected.no_suitable
    count = db.execute("SELECT count(*) FROM match_results WHERE run_id=?", (run_id,)).fetchone()[0]
    db.execute(
        "UPDATE match_runs SET demands_assessed=?,pairs_scored=?,roles_without_candidate=?,"
        "dq_summary_json=? WHERE run_id=?",
        (len(demands), count, without, dumps(summary), run_id),
    )
