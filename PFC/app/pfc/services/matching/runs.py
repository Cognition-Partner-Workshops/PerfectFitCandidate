"""Queued scoring and durable snapshots (REQ-MAT-03/08, REQ-ELIG-08, REQ-SCR-04)."""

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from typing import Any, Literal

from pfc.config import Settings
from pfc.db.connection import database
from pfc.domain.config_loader import DomainConfig, ScoringTemplate, load_domain_config
from pfc.domain.normalise import ParsedDate, normalise_term, parse_date
from pfc.domain.scoring import (
    ENGINE_VERSION,
    Demand,
    MatchRun,
    RequirementItem,
    Resource,
    SkillBasis,
    SkillEvidence,
    build_ct_basis,
    run_match,
)
from pfc.jobs.runner import JobCancelled, JobContext, JobRunner

from .common import audit, digest, dumps, timestamp
from .ports import MatchingPorts
from .results import _mask_email
from .semantic import VectorCache, model_identity, semantic_inputs


def utc_now() -> datetime:
    return datetime.now(UTC)


def _template(
    db: sqlite3.Connection, config: DomainConfig, now: datetime
) -> tuple[int, ScoringTemplate]:
    row = db.execute(
        "SELECT * FROM scoring_templates WHERE name='default' AND status='active' "
        "ORDER BY version DESC,template_id DESC LIMIT 1",
    ).fetchone()
    if row:
        rules = json.loads(row["rules_json"])
        rules["weights"] = json.loads(row["weights_json"])
        rules.update(name=row["name"], version=row["version"])
        return row["template_id"], ScoringTemplate.from_dict(rules)
    template = config.template
    payload = template.model_dump(mode="json")
    existing = db.execute(
        "SELECT template_id FROM scoring_templates WHERE name=? AND version=?",
        (template.name, template.version),
    ).fetchone()
    if existing:
        raise ValueError(
            "Default template version already exists but is not active; activate it first"
        )
    cursor = db.execute(
        "INSERT INTO scoring_templates(name,version,weights_json,rules_json,status,created_at,"
        "activated_at) VALUES (?,?,?,?,'active',?,?)",
        (
            template.name,
            template.version,
            dumps(payload["weights"]),
            dumps(payload),
            timestamp(now),
            timestamp(now),
        ),
    )
    return int(cursor.lastrowid or 0), template


def _embedding_model(db: sqlite3.Connection, identity: dict[str, Any]) -> int:
    active = db.execute("SELECT 1 FROM embedding_models WHERE is_active=1").fetchone()
    db.execute(
        "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version,is_active)"
        " VALUES (:name,:runtime,:file_sha256,:dim,:pipeline_version,0) "
        "ON CONFLICT(name,file_sha256,pipeline_version) DO NOTHING",
        identity,
    )
    row = db.execute(
        "SELECT * FROM embedding_models WHERE name=? AND file_sha256=? AND pipeline_version=?",
        (identity["name"], identity["file_sha256"], identity["pipeline_version"]),
    ).fetchone()
    if row["dim"] != identity["dim"] or row["runtime"] != identity["runtime"]:
        raise ValueError("Embedding model identity conflicts with the recorded dimension/runtime")
    if not active:
        db.execute(
            "UPDATE embedding_models SET is_active=1 WHERE embedding_model_id=?",
            (row["embedding_model_id"],),
        )
    return int(row["embedding_model_id"])


def _inputs(
    db: sqlite3.Connection,
    scope: str,
    demand_ids: Sequence[int],
    config: DomainConfig,
) -> tuple[
    list[Demand], list[Resource], dict[str, list[SkillBasis]], dict[str, dict[str, Any]], list[int]
]:
    selected = sorted(set(demand_ids))
    rows = db.execute(
        "SELECT d.*,v.jd_text_norm,q.jd_req_id,q.location_norm,q.onsite_required,q.extracted_by "
        "FROM demands d LEFT JOIN demand_versions v ON v.demand_version_id=d.current_version_id "
        "LEFT JOIN jd_requirements q ON q.demand_version_id=v.demand_version_id "
        "AND q.status='confirmed' WHERE d.lifecycle='open' ORDER BY d.demand_id",
    ).fetchall()
    demands, metadata, included = [], {}, set()
    for row in rows:
        if scope == "demands" and row["demand_id"] not in selected:
            continue
        if row["jd_req_id"] is None:
            continue
        items = db.execute(
            "SELECT i.*,s.name AS canonical FROM jd_requirement_items i "
            "LEFT JOIN skills s ON s.skill_id=i.canonical_skill_id "
            "WHERE i.jd_req_id=? ORDER BY i.position,i.item_id",
            (row["jd_req_id"],),
        ).fetchall()
        requirements = tuple(
            RequirementItem(
                str(item["item_id"]),
                item["text"],
                "preferred" if item["kind"] in {"preferred_skill", "domain"} else "mandatory",
                item["canonical"] or config.synonyms.resolve(item["text"]),
            )
            for item in items
        )
        demands.append(
            Demand(
                row["code_requisition"],
                parse_date(row["position_start_date"]),
                row["code_local_grade"],
                row["location_norm"] or row["region"],
                bool(row["onsite_required"]),
                requirements,
            )
        )
        metadata[row["code_requisition"]] = dict(row)
        included.add(row["demand_id"])
    skipped = sorted(
        set(selected if scope == "demands" else [r["demand_id"] for r in rows]) - included
    )
    resources = []
    for row in db.execute("SELECT * FROM resources WHERE lifecycle='active' ORDER BY resource_id"):
        end = parse_date(row["bench_end_date"])
        if row["bench_end_date_parse_status"] == "unparseable":
            end = ParsedDate(
                None, "unparseable", row["bench_end_date"], "Bench End Date unparseable"
            )
        resources.append(
            Resource(
                str(row["resource_id"]),
                row["short_name"],
                row["grade"],
                row["location"],
                row["bench_status"],
                row["group_name"],
                parse_date(row["bench_start_date"]),
                end,
                row["skills_raw"],
                f"candidate:{row['candidate_id']}"
                if row["candidate_id"]
                else f"resource:{row['resource_id']}",
            )
        )
    cv_bases: dict[str, list[SkillBasis]] = {}
    versions = db.execute(
        "SELECT * FROM cv_versions WHERE candidate_id IS NOT NULL "
        "ORDER BY candidate_id,version_no,cv_version_id",
    ).fetchall()
    active_candidates = {r.candidate_id for r in resources}
    for version in versions:
        candidate = f"candidate:{version['candidate_id']}"
        if candidate not in active_candidates:
            continue
        skills = db.execute(
            "SELECT rs.*,s.name AS canonical FROM resource_skills rs "
            "LEFT JOIN skills s ON s.skill_id=rs.canonical_skill_id "
            "WHERE rs.cv_version_id=? AND rs.source='CV' ORDER BY rs.resource_skill_id",
            (version["cv_version_id"],),
        ).fetchall()
        evidence = tuple(
            SkillEvidence(
                item["skill_raw"],
                normalise_term(item["skill_norm"]),
                item["canonical"] or config.synonyms.resolve(item["skill_raw"]),
                item["evidence"] or item["skill_raw"],
            )
            for item in skills
        )
        cv_bases.setdefault(candidate, []).append(
            SkillBasis(
                f"CV:{version['cv_version_id']}",
                f"CV v{version['version_no']}",
                evidence,
                bool(version["is_latest"]),
            )
        )
    return demands, resources, cv_bases, metadata, skipped


def _cv_id(basis_id: str) -> int | None:
    return int(basis_id.split(":")[1]) if basis_id.startswith("CV:") else None


def _persist(
    db: sqlite3.Connection,
    run_id: int,
    result: MatchRun,
    metadata: dict[str, dict[str, Any]],
    config: DomainConfig,
    skipped: list[int],
    display_emails: dict[str, str | None],
) -> None:
    demand_ids = {code: row["demand_id"] for code, row in metadata.items()}
    for exclusion in (*result.global_exclusions, *result.pair_exclusions):
        for reason in exclusion.reasons:
            db.execute(
                "INSERT INTO eligibility_exclusions"
                "(run_id,resource_id,demand_id,reason_code,detail)"
                " VALUES (?,?,?,?,?)",
                (
                    run_id,
                    int(exclusion.resource_id),
                    demand_ids.get(exclusion.code_requisition or ""),
                    reason.code,
                    reason.detail,
                ),
            )
    summary: dict[str, Any] = {"skipped_demand_ids": skipped, "demands": {}}
    for demand_result in result.demands:
        demand = demand_result.demand
        meta = metadata[demand.code_requisition]
        summary["demands"][str(meta["demand_id"])] = {
            "no_suitable_candidate": demand_result.no_suitable_candidate,
            "weak_only": demand_result.weak_only,
            "flags": list(demand_result.flags),
        }
        shortlist = {row.resource.resource_id for row in demand_result.shortlist}
        emails = {row.resource.resource_id for row in demand_result.email_preselected}
        for rank, row in enumerate((*demand_result.rows, *demand_result.insufficient_data), 1):
            person, best = row.resource, row.best
            relation = "exact"
            if row.grade_distance:
                relation = (
                    "adjacent_above"
                    if config.grades.ordinal(person.grade or "")
                    > config.grades.ordinal(demand.grade or "")
                    else "adjacent_below"
                )
            cursor = db.execute(
                "INSERT INTO match_results"
                "(run_id,demand_id,demand_version_id,jd_req_id,resource_id,"
                "candidate_id,best_cv_version_id,score,band,rank,shortlisted,email_preselected,"
                "grade_relation,location_assessment,status_priority,mandatory_missing,"
                "matching_evidence,gaps_checks,eligibility_evidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    meta["demand_id"],
                    meta["current_version_id"],
                    meta["jd_req_id"],
                    int(person.resource_id),
                    int(person.candidate_id.split(":")[1])
                    if person.candidate_id and person.candidate_id.startswith("candidate:")
                    else None,
                    _cv_id(best.basis_id),
                    best.score,
                    best.band,
                    rank,
                    person.resource_id in shortlist,
                    person.resource_id in emails,
                    relation,
                    dict(best.components["location"].inputs)["classification"],
                    row.status_priority,
                    best.mandatory_missing,
                    best.matching_evidence,
                    row.gaps_checks,
                    best.eligibility_evidence,
                ),
            )
            result_id = int(cursor.lastrowid or 0)
            for basis in row.per_basis:
                db.execute(
                    "INSERT INTO match_cv_scores(result_id,cv_version_id,score,band)"
                    " VALUES (?,?,?,?)",
                    (result_id, _cv_id(basis.basis_id), basis.score, basis.band),
                )
            for name, component in best.components.items():
                inputs: dict[str, Any] = dict(component.inputs)
                if name == "skills":
                    inputs["items"] = [asdict(item) for item in best.items]
                    inputs["display_facts"] = {
                        "requisition": demand.code_requisition,
                        "start_date": meta["position_start_date"],
                        "account": meta["account_name"],
                        "role": meta["demand_request_name"],
                        "demand_grade": demand.grade,
                        "role_location": demand.region or "Unspecified",
                        "potential_person": person.short_name,
                        "person_email": display_emails[person.resource_id],
                        "person_grade": person.grade,
                        "person_location": person.location,
                        "status": person.bench_status,
                    }
                db.execute(
                    "INSERT INTO score_breakdowns VALUES (?,?,?,?,?)",
                    (result_id, name, component.points, component.maximum, dumps(inputs)),
                )
    db.execute(
        "UPDATE match_runs SET demands_assessed=?,pairs_scored=?,roles_without_candidate=?,"
        "dq_summary_json=? WHERE run_id=?",
        (
            result.requisitions_assessed,
            result.pairs_scored,
            result.roles_without_suitable_candidate,
            dumps(summary),
            run_id,
        ),
    )


def start_run(
    settings: Settings,
    runner: JobRunner,
    ports: MatchingPorts,
    *,
    scope: Literal["full", "demands"] = "full",
    demand_ids: Sequence[int] = (),
    user_id: int | None = None,
    as_of_date: date | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[int, int]:
    """REQ-MAT-08: freeze inputs before dispatch; the worker owns all its connections."""
    cache = VectorCache(ports)
    identity = model_identity(cache.embedder)
    if scope not in {"full", "demands"} or (scope == "demands" and not demand_ids):
        raise ValueError("Scope must be full or demands with at least one demand ID")
    now = clock()
    as_of_date = as_of_date or now.date()
    config = load_domain_config()
    with database(settings) as db:
        template_id, template = _template(db, config, now)
        model_id = _embedding_model(db, identity)
        demands, resources, cvs, metadata, skipped = _inputs(db, scope, demand_ids, config)
        display_emails = {
            str(row["resource_id"]): _mask_email(row["email_address"])
            for row in db.execute(
                "SELECT resource_id,email_address FROM resources WHERE lifecycle='active' "
                "ORDER BY resource_id"
            )
        }
        synonyms = [asdict(row) for row in config.synonyms.rows if row.status == "approved"]
        snapshot = {
            "template": template.model_dump(mode="json"),
            "grades": config.grades.model_dump(mode="json"),
            "statuses": config.bench_statuses.model_dump(mode="json"),
            "locations": config.locations.model_dump(mode="json"),
            "synonyms": synonyms,
            "as_of_date": as_of_date.isoformat(),
            "embedding": identity,
            "scope": scope,
            "demand_ids": sorted(set(demand_ids)),
            "demands": metadata,
            "requirements": [asdict(demand)["requirements"] for demand in demands],
            "resources": [repr(resource) for resource in resources],
            "display_emails": display_emails,
            "cv_bases": {key: [asdict(basis) for basis in value] for key, value in cvs.items()},
        }
        llm_models = sorted({m["extracted_by"] for m in metadata.values() if m["extracted_by"]})
        cursor = db.execute(
            "INSERT INTO match_runs(scope,scope_json,template_id,embedding_model_id,llm_model,"
            "engine_version,synonyms_version,config_hash,triggered_by,status,dq_summary_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,'queued',?)",
            (
                scope,
                dumps(
                    {"demand_ids": sorted(set(demand_ids)), "as_of_date": as_of_date.isoformat()}
                ),
                template_id,
                model_id,
                ", ".join(llm_models) or None,
                ENGINE_VERSION,
                digest(synonyms),
                digest(snapshot),
                user_id,
                dumps({"skipped_demand_ids": skipped}),
            ),
        )
        run_id = int(cursor.lastrowid or 0)
        audit(db, "match.start", "match_runs", run_id, user_id, now, scope=scope)

    def finish(status: str) -> None:
        with database(settings) as db:
            db.execute(
                "UPDATE match_runs SET status=?,finished_at=? WHERE run_id=?",
                (status, timestamp(clock()), run_id),
            )

    def work(context: JobContext) -> None:
        try:
            context.raise_if_cancelled()
            with database(settings) as db:
                db.execute(
                    "UPDATE match_runs SET status='running',started_at=? WHERE run_id=?",
                    (timestamp(clock()), run_id),
                )
            bases = [build_ct_basis(resource, config) for resource in resources]
            bases += [basis for candidate_bases in cvs.values() for basis in candidate_bases]
            semantics = semantic_inputs(
                demands, metadata, bases, cache, template, context.report_progress
            )
            context.raise_if_cancelled()
            result = run_match(
                demands, resources, cvs, semantics, config, as_of_date, template=template
            )
            if len({r.candidate_id for r in resources}) < len(resources):
                # Persist every scored pair; only the domain's unique shortlist is selected.
                originals = {r.resource_id: r for r in resources}
                expanded = [replace(r, candidate_id=f"row:{r.resource_id}") for r in resources]
                expanded_cvs = {
                    f"row:{r.resource_id}": cvs.get(r.candidate_id or "", []) for r in resources
                }
                all_pairs = run_match(
                    demands,
                    expanded,
                    expanded_cvs,
                    semantics,
                    config,
                    as_of_date,
                    template=template,
                )
                full_by_code = {d.demand.code_requisition: d for d in all_pairs.demands}
                result = replace(
                    result,
                    demands=tuple(
                        replace(
                            d,
                            rows=tuple(
                                replace(row, resource=originals[row.resource.resource_id])
                                for row in full_by_code[d.demand.code_requisition].rows
                            ),
                            insufficient_data=tuple(
                                replace(row, resource=originals[row.resource.resource_id])
                                for row in full_by_code[d.demand.code_requisition].insufficient_data
                            ),
                        )
                        for d in result.demands
                    ),
                )
            with database(settings) as db:
                context.raise_if_cancelled()
                _persist(db, run_id, result, metadata, config, skipped, display_emails)
                db.execute(
                    "UPDATE match_runs SET status='completed',finished_at=? WHERE run_id=?",
                    (timestamp(clock()), run_id),
                )
                context.raise_if_cancelled()
        except JobCancelled:
            finish("cancelled")
            raise
        except Exception:
            finish("failed")
            raise

    try:
        job_id = runner.submit("match_run", work, {"run_id": run_id}, created_by=user_id)
    except Exception:
        finish("failed")
        raise
    with database(settings) as db:
        db.execute(
            "UPDATE match_runs SET scope_json=json_set(scope_json,'$.job_id',?) WHERE run_id=?",
            (job_id, run_id),
        )
    return run_id, job_id
