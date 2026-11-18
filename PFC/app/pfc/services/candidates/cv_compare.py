"""REQ-IN-24 / FR-ING-52: read-only side-by-side comparison of one candidate's CV versions.

Nothing here changes match results, CV versions or skills. Facts and text are masked on read;
what-if scores re-run the deterministic engine for each version as if it were the latest CV.
"""

import difflib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from pfc.config import Settings
from pfc.db.connection import database
from pfc.domain.config_loader import DomainConfig, ScoringTemplate
from pfc.domain.cv_profile import degree, experience_years
from pfc.domain.normalise import normalise_term
from pfc.domain.scoring import (
    BasisResult,
    Demand,
    MatchRun,
    Resource,
    SkillBasis,
    build_combined_basis,
    run_match,
)
from pfc.jobs.runner import JobCancelled, JobContext, JobRunner
from pfc.security.problems import Problem
from pfc.services.ingestion.cv_facts import public_facts
from pfc.services.ingestion.masking import mask_evidence
from pfc.services.matching.common import audit, dumps, timestamp
from pfc.services.matching.current import current_runs, run_pair_predicate
from pfc.services.matching.cv_bases import canonical_skill_ids, load_resources, version_basis
from pfc.services.matching.ports import MatchingPorts
from pfc.services.matching.runs import load_demands
from pfc.services.matching.semantic import VectorCache, semantic_inputs
from pfc.services.occ.domain import active_template, occ_domain_config
from pfc.services.pii import mask_emails_in

JOB_KIND = "cv_compare"
MAX_DIFF_ROWS = 2000
CONTEXT_LINES = 2
_STATUS_ORDER = {"added": 0, "removed": 1, "changed": 2, "unchanged": 3}


def utc_now() -> datetime:
    return datetime.now(UTC)


def _pair(
    db: sqlite3.Connection, candidate_id: int, left: int, right: int
) -> tuple[sqlite3.Row, sqlite3.Row]:
    if (
        db.execute("SELECT 1 FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        is None
    ):
        raise Problem(404, "CANDIDATE_NOT_FOUND", "Candidate not found")
    if left == right:
        raise Problem(422, "CV_COMPARE_SAME_VERSION", "Choose two different CV versions")
    rows = []
    for cv_version_id in (left, right):
        row = db.execute(
            "SELECT v.*,r.file_name,r.file_mtime FROM cv_versions v "
            "JOIN resumes r ON r.resume_id=v.resume_id "
            "WHERE v.cv_version_id=? AND v.candidate_id=?",
            (cv_version_id, candidate_id),
        ).fetchone()
        if row is None:
            raise Problem(404, "CV_VERSION_NOT_FOUND", "CV version not found for this candidate")
        rows.append(row)
    return rows[0], rows[1]


def _removed(row: sqlite3.Row) -> bool:
    return row["extraction_removed_at"] is not None


def _meta(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cv_version_id": row["cv_version_id"],
        "version_no": row["version_no"],
        "file_name": mask_emails_in(row["file_name"]),
        "modified_at": row["file_mtime"],
        "extracted_at": row["extracted_at"],
        "is_latest": bool(row["is_latest"]),
        "extraction_removed": _removed(row),
    }


def _diff(
    left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for key in set(left) | set(right):
        before, after = left.get(key), right.get(key)
        status = "unchanged" if before and after else "removed" if before else "added"
        label = (after or before or {}).get("value", key)
        rows.append({"key": key, "label": label, "status": status, "left": before, "right": after})
    return sorted(rows, key=lambda row: (_STATUS_ORDER[row["status"]], row["label"].casefold()))


def _skills(basis: SkillBasis | None) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in basis.skills if basis else ():
        key = normalise_term(item.canonical or item.normalised)
        found.setdefault(
            key,
            {
                "value": item.canonical or item.raw,
                "raw": item.raw,
                "evidence": mask_evidence(item.evidence),
                "source": item.source or "CV",
            },
        )
    return found


def _fact_list(facts: Any, name: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for fact in getattr(facts, name, None) or ():
        found.setdefault(
            normalise_term(fact.value),
            {"value": fact.value, "evidence": fact.evidence, "source": fact.source},
        )
    return found


def _degree(text: str) -> dict[str, Any] | None:
    best = degree(text)
    if best is None:
        return None
    line = next((line for line in text.splitlines() if degree(line) == best), "")
    return {
        "value": best.label + (" (technical)" if best.technical else ""),
        "evidence": mask_evidence(line.strip()),
    }


def _scalar(left: Any, right: Any) -> str:
    if left == right:
        return "unchanged"
    if left is None:
        return "added"
    if right is None:
        return "removed"
    return "changed"


def _facts(
    left: sqlite3.Row,
    right: sqlite3.Row,
    left_basis: SkillBasis | None,
    right_basis: SkillBasis | None,
    as_of: date,
) -> dict[str, Any]:
    texts = ["" if _removed(row) else row["text_norm"] or "" for row in (left, right)]
    facts = [None if _removed(row) else public_facts(row["facts_json"]) for row in (left, right)]
    years = [experience_years(text, as_of) if text else None for text in texts]
    degrees = [_degree(text) for text in texts]
    single = []
    for name in ("name", "phone"):
        values = [getattr(fact, name, None) for fact in facts]
        pair = [
            {"value": value.value, "evidence": value.evidence, "source": value.source}
            if value
            else None
            for value in values
        ]
        status = _scalar(*(item["value"] if item else None for item in pair))
        single.append({"key": name, "status": status, "left": pair[0], "right": pair[1]})
    year_values = [
        {"value": f"{value:g} years", "evidence": "Total experience read from dated CV text"}
        if value is not None
        else None
        for value in years
    ]
    single.append(
        {
            "key": "years_of_experience",
            "status": _scalar(*years),
            "left": year_values[0],
            "right": year_values[1],
        }
    )
    single.append(
        {
            "key": "degree",
            "status": _scalar(*(item["value"] if item else None for item in degrees)),
            "left": degrees[0],
            "right": degrees[1],
        }
    )
    return {
        "summary": single,
        "years_delta": None if None in years else round((years[1] or 0) - (years[0] or 0), 1),
        "skills": _diff(_skills(left_basis), _skills(right_basis)),
        "experience": _diff(*(_fact_list(fact, "experience") for fact in facts)),
        "qualifications": _diff(*(_fact_list(fact, "qualifications") for fact in facts)),
        "projects": _diff(*(_fact_list(fact, "projects") for fact in facts)),
        "facts_extracted": [fact is not None for fact in facts],
    }


def _line(lines: list[str], index: int | None) -> dict[str, Any] | None:
    return None if index is None else {"no": index + 1, "text": lines[index]}


def text_diff(left: str, right: str) -> dict[str, Any]:
    """Line diff of masked CV text; long unchanged runs collapse to a skip row."""
    a = [mask_evidence(line) for line in left.splitlines()]
    b = [mask_evidence(line) for line in right.splitlines()]
    rows: list[dict[str, Any]] = []
    added = removed = unchanged = 0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            count = i2 - i1
            unchanged += count
            head = 0 if i1 == 0 else CONTEXT_LINES
            tail = 0 if i2 == len(a) else CONTEXT_LINES
            if count > head + tail + 1:
                keep = [*range(head), *range(count - tail, count)]
                for offset in keep[:head]:
                    rows.append(
                        {
                            "op": "equal",
                            "left": _line(a, i1 + offset),
                            "right": _line(b, j1 + offset),
                        }
                    )
                rows.append(
                    {"op": "skip", "count": count - head - tail, "left": None, "right": None}
                )
                for offset in keep[head:]:
                    rows.append(
                        {
                            "op": "equal",
                            "left": _line(a, i1 + offset),
                            "right": _line(b, j1 + offset),
                        }
                    )
            else:
                for offset in range(count):
                    rows.append(
                        {
                            "op": "equal",
                            "left": _line(a, i1 + offset),
                            "right": _line(b, j1 + offset),
                        }
                    )
            continue
        removed += i2 - i1
        added += j2 - j1
        for offset in range(max(i2 - i1, j2 - j1)):
            li = i1 + offset if i1 + offset < i2 else None
            rj = j1 + offset if j1 + offset < j2 else None
            op = (
                "changed"
                if li is not None and rj is not None
                else "removed"
                if rj is None
                else "added"
            )
            rows.append({"op": op, "left": _line(a, li), "right": _line(b, rj)})
    return {
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "rows": rows[:MAX_DIFF_ROWS],
        "truncated": len(rows) > MAX_DIFF_ROWS,
    }


def _current_results(db: sqlite3.Connection, candidate_id: int) -> list[sqlite3.Row]:
    predicate, params = run_pair_predicate(current_runs(db))
    return db.execute(
        "SELECT m.result_id,m.run_id,m.demand_id,m.resource_id,m.score,m.band,"
        "m.best_cv_version_id,d.code_requisition,d.demand_request_name,r.short_name,"
        "json_extract(mr.scope_json,'$.as_of_date') AS as_of_date "
        "FROM match_results m JOIN demands d ON d.demand_id=m.demand_id "
        "JOIN resources r ON r.resource_id=m.resource_id "
        "JOIN match_runs mr ON mr.run_id=m.run_id "
        f"WHERE r.candidate_id=? AND {predicate} ORDER BY d.code_requisition,m.resource_id",
        (candidate_id, *params),
    ).fetchall()


def _stored(
    db: sqlite3.Connection, candidate_id: int, left: int, right: int
) -> list[dict[str, Any]]:
    rows = []
    for row in _current_results(db, candidate_id):
        scores = {
            item["cv_version_id"]: {"score": item["score"], "band": item["band"]}
            for item in db.execute(
                "SELECT cv_version_id,score,band FROM match_cv_scores "
                "WHERE result_id=? AND cv_version_id IN (?,?)",
                (row["result_id"], left, right),
            )
        }
        rows.append(
            {
                "result_id": row["result_id"],
                "run_id": row["run_id"],
                "demand_id": row["demand_id"],
                "code_requisition": row["code_requisition"],
                "role": row["demand_request_name"],
                "resource_id": row["resource_id"],
                "short_name": row["short_name"],
                "score": row["score"],
                "band": row["band"],
                "best_cv_version_id": row["best_cv_version_id"],
                "left": scores.get(left),
                "right": scores.get(right),
            }
        )
    return rows


def compare(settings: Settings, candidate_id: int, left: int, right: int) -> dict[str, Any]:
    """Facts, masked text diff and stored current-demand scores for two versions."""
    with database(settings) as db:
        left_row, right_row = _pair(db, candidate_id, left, right)
        config = occ_domain_config(db)
        canonical = canonical_skill_ids(db)
        resource_ids = [
            row[0]
            for row in db.execute(
                "SELECT resource_id FROM resources WHERE candidate_id=?", (candidate_id,)
            )
        ]
        bases = [
            None if _removed(row) else version_basis(db, config, row, canonical, resource_ids)
            for row in (left_row, right_row)
        ]
        texts = ["" if _removed(row) else row["text_norm"] or "" for row in (left_row, right_row)]
        return {
            "candidate_id": candidate_id,
            "left": _meta(left_row),
            "right": _meta(right_row),
            "facts": _facts(left_row, right_row, bases[0], bases[1], utc_now().date()),
            "text_diff": text_diff(*texts),
            "stored_scores": _stored(db, candidate_id, left, right),
        }


def _scored(result: BasisResult, demand: Demand) -> dict[str, Any]:
    texts = {item.item_id: item.text for item in demand.requirements}
    return {
        "score": result.score,
        "band": result.band,
        "basis": result.label,
        "components": [
            {"component": name, "points": component.points, "max_points": component.maximum}
            for name, component in result.components.items()
        ],
        "items": [
            {
                "item_id": item.item_id,
                "text": mask_evidence(texts.get(item.item_id, item.item_id)),
                "kind": item.kind,
                "credit": item.credit,
                "method": item.method,
                "evidence": mask_evidence(item.evidence),
            }
            for item in result.items
        ],
        "mandatory_missing": result.mandatory_missing,
        "gaps": [mask_evidence(gap) for gap in result.gaps],
        "excluded": False,
        "reasons": [],
    }


def _outcome(run: MatchRun, resource: Resource, demand: Demand) -> dict[str, Any]:
    exclusions = [
        exclusion
        for exclusion in (*run.global_exclusions, *run.pair_exclusions)
        if exclusion.resource_id == resource.resource_id
        and exclusion.code_requisition in (None, demand.code_requisition)
    ]
    if exclusions:
        return {
            "score": None,
            "band": None,
            "basis": None,
            "components": [],
            "items": [],
            "mandatory_missing": 0,
            "gaps": [],
            "excluded": True,
            "reasons": [
                {"code": reason.code, "detail": mask_evidence(reason.detail)}
                for exclusion in exclusions
                for reason in exclusion.reasons
            ],
        }
    for result in run.demands:
        if result.demand.code_requisition != demand.code_requisition:
            continue
        for row in (*result.rows, *result.insufficient_data):
            if row.resource.resource_id == resource.resource_id:
                return _scored(row.best, demand)
    raise ValueError("Scored pair missing from engine output")


def score_versions(
    demands: list[Demand],
    metadata: dict[str, dict[str, Any]],
    resources: list[Resource],
    bases: dict[int, SkillBasis],
    as_of: dict[str, date],
    config: DomainConfig,
    template: ScoringTemplate,
    cache: VectorCache,
    progress: Callable[[float, str], None],
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Score each version alone as the latest CV; keyed by (requisition, resource, version)."""
    combined = [
        build_combined_basis(resource, [basis], config)
        for resource in resources
        for basis in bases.values()
    ]
    semantics = semantic_inputs(demands, metadata, combined, cache, template, progress)
    outcomes = {}
    for when in sorted(set(as_of.values())):
        group = [demand for demand in demands if as_of[demand.code_requisition] == when]
        for resource in resources:
            for cv_version_id, basis in bases.items():
                run = run_match(
                    group,
                    [resource],
                    {resource.candidate_id or resource.resource_id: [basis]},
                    semantics,
                    config,
                    when,
                    template=template,
                )
                for demand in group:
                    key = (demand.code_requisition, resource.resource_id, cv_version_id)
                    outcomes[key] = _outcome(run, resource, demand)
    return outcomes


def start_scoring(
    settings: Settings,
    runner: JobRunner,
    ports: MatchingPorts,
    candidate_id: int,
    left: int,
    right: int,
    *,
    user_id: int | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[int, int]:
    """Freeze inputs, then score both versions in a `cv_compare` job; never touches results."""
    cache = VectorCache(ports)
    now = clock()
    with database(settings) as db:
        rows = _pair(db, candidate_id, left, right)
        if any(_removed(row) for row in rows):
            raise Problem(409, "CV_EXTRACTION_REMOVED", "A removed CV extraction cannot be scored")
        config = occ_domain_config(db)
        _, template = active_template(db)
        current = _current_results(db, candidate_id)
        resource_ids = sorted({row["resource_id"] for row in current})
        demand_ids = sorted({row["demand_id"] for row in current})
        stored = {(row["code_requisition"], str(row["resource_id"])): row for row in current}
        demands, metadata, skipped = (
            load_demands(db, "demands", demand_ids, config) if demand_ids else ([], {}, [])
        )
        resources = load_resources(db, resource_ids)
        canonical = canonical_skill_ids(db)
        bases = {
            row["cv_version_id"]: replace(
                version_basis(db, config, row, canonical, resource_ids), is_latest=True
            )
            for row in rows
        }
        as_of = {}
        for demand in demands:
            values = [
                row["as_of_date"]
                for (code, _), row in stored.items()
                if code == demand.code_requisition and row["as_of_date"]
            ]
            as_of[demand.code_requisition] = date.fromisoformat(values[0]) if values else now.date()
        comparison_id = db.execute(
            "INSERT INTO cv_comparisons(candidate_id,left_cv_version_id,right_cv_version_id,"
            "status,created_by,created_at) VALUES (?,?,?,'queued',?,?)",
            (candidate_id, left, right, user_id, timestamp(now)),
        ).lastrowid
        assert comparison_id is not None
        audit(
            db,
            "cv.compare_score",
            "cv_comparisons",
            comparison_id,
            user_id,
            now,
            candidate_id=candidate_id,
            left_cv_version_id=left,
            right_cv_version_id=right,
            demands=len(demands),
        )

    def finish(status: str, result: dict[str, Any] | None = None) -> None:
        with database(settings) as db:
            db.execute(
                "UPDATE cv_comparisons SET status=?,result_json=?,finished_at=? "
                "WHERE comparison_id=? AND status IN ('queued','running')",
                (
                    status,
                    dumps(result) if result is not None else None,
                    timestamp(clock()),
                    comparison_id,
                ),
            )

    def work(context: JobContext) -> None:
        try:
            context.raise_if_cancelled()
            with database(settings) as db:
                db.execute(
                    "UPDATE cv_comparisons SET status='running' WHERE comparison_id=?",
                    (comparison_id,),
                )
            outcomes = score_versions(
                demands,
                metadata,
                resources,
                bases,
                as_of,
                config,
                template,
                cache,
                context.report_progress,
            )
            context.raise_if_cancelled()
            rows = []
            for demand in demands:
                for resource in resources:
                    row = stored.get((demand.code_requisition, resource.resource_id))
                    if row is None:
                        continue
                    before = outcomes[(demand.code_requisition, resource.resource_id, left)]
                    after = outcomes[(demand.code_requisition, resource.resource_id, right)]
                    delta = (
                        None
                        if before["score"] is None or after["score"] is None
                        else round(after["score"] - before["score"], 2)
                    )
                    rows.append(
                        {
                            "demand_id": row["demand_id"],
                            "code_requisition": demand.code_requisition,
                            "role": row["demand_request_name"],
                            "resource_id": row["resource_id"],
                            "short_name": row["short_name"],
                            "as_of_date": as_of[demand.code_requisition].isoformat(),
                            "stored_score": row["score"],
                            "stored_band": row["band"],
                            "stored_best_cv_version_id": row["best_cv_version_id"],
                            "left": before,
                            "right": after,
                            "delta": delta,
                        }
                    )
            finish(
                "completed",
                {
                    "template_name": template.name,
                    "template_version": template.version,
                    "skipped_demand_ids": skipped,
                    "demands": rows,
                },
            )
        except JobCancelled:
            finish("cancelled")
            raise
        except Exception:
            finish("failed")
            raise

    params = {
        "candidate_id": candidate_id,
        "comparison_id": comparison_id,
        "left_cv_version_id": left,
        "right_cv_version_id": right,
    }
    try:
        job_id = runner.submit(JOB_KIND, work, params, created_by=user_id)
    except Exception:
        finish("failed")
        raise
    with database(settings) as db:
        db.execute(
            "UPDATE cv_comparisons SET job_id=? WHERE comparison_id=?", (job_id, comparison_id)
        )
    return comparison_id, job_id


def latest_scoring(
    settings: Settings, candidate_id: int, left: int, right: int
) -> dict[str, Any] | None:
    """The newest what-if scoring for this exact pair, or None when never scored."""
    with database(settings) as db:
        _pair(db, candidate_id, left, right)
        row = db.execute(
            "SELECT * FROM cv_comparisons WHERE candidate_id=? AND left_cv_version_id=? "
            "AND right_cv_version_id=? ORDER BY comparison_id DESC LIMIT 1",
            (candidate_id, left, right),
        ).fetchone()
    if row is None:
        return None
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {
        "comparison_id": row["comparison_id"],
        "job_id": row["job_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "result": result,
    }
