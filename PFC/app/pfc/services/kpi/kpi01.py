"""KPI-01 measurement over persisted runs and analyst labels; no database writes."""

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from pfc.services.matching.results import _mask_email, active_overrides, apply_overrides

Profile = tuple[str, int]


@dataclass(frozen=True)
class Label:
    requisition: str
    email: str
    split: str
    line: int


@dataclass(frozen=True)
class Resolved:
    split: str
    profiles: frozenset[Profile]
    no_suitable: bool


def parse_labels(source: TextIO) -> list[Label]:
    reader = csv.DictReader(source, strict=True)
    labels = []
    try:
        if len(reader.fieldnames or []) != 3 or set(reader.fieldnames or []) != {
            "code_requisition",
            "email",
            "split",
        }:
            raise ValueError("CSV header must contain code_requisition,email,split")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Invalid CSV row at line {reader.line_num}")
            code, email = row["code_requisition"].strip(), row["email"]
            split = row["split"].strip() or "holdout"
            if not code:
                raise ValueError(f"Missing requisition at line {reader.line_num}")
            if split not in {"tune", "holdout"}:
                raise ValueError(f"Invalid split at line {reader.line_num}: use tune or holdout")
            labels.append(Label(code, email, split, reader.line_num))
    except csv.Error:
        raise ValueError(f"Invalid CSV at line {reader.reader.line_num}") from None
    return labels


def _profile(candidate_id: int | None, resource_id: int) -> Profile:
    return ("candidate", candidate_id) if candidate_id is not None else ("resource", resource_id)


def _resolve(
    labels: list[Label],
    emails: dict[str, set[Profile]],
    assessed: dict[str, int],
    analyst: str,
    issues: list[dict[str, Any]],
) -> dict[str, Resolved]:
    grouped: dict[str, list[Label]] = defaultdict(list)
    for label in labels:
        grouped[label.requisition].append(label)
    resolved = {}
    for code, rows in grouped.items():

        def issue(kind: str, label: Label | None = None, *, requisition: str = code) -> None:
            item: dict[str, Any] = {"analyst": analyst, "requisition": requisition, "code": kind}
            if label is not None:
                item.update(line=label.line, email=_mask_email(label.email))
            issues.append(item)

        splits = {row.split for row in rows}
        split = next(iter(splits)) if len(splits) == 1 else "holdout"
        if len(splits) > 1:
            issue("conflicting_splits")
        if code not in assessed:
            issue("not_assessed_by_run")
        real = [row for row in rows if row.email]
        if real and len(real) != len(rows):
            issue("empty_label_ignored")
        profiles: set[Profile] = set()
        for row in real:
            matches = emails.get(row.email.casefold(), set())
            if len(matches) != 1:
                issue("unresolved_email" if not matches else "ambiguous_email", row)
            else:
                profiles.update(matches)
        if real and not profiles:
            issue("no_resolved_labels")
        resolved[code] = Resolved(split, frozenset(profiles), not real)
    return resolved


def _comparison(
    system: set[Profile] | None, reference: Resolved, agreement: bool, eligible: bool
) -> dict[str, Any]:
    status = (
        "excluded"
        if not eligible or system is None
        else ("no_suitable" if reference.no_suitable else "scored")
    )
    hits = len(system & reference.profiles) if system is not None and status == "scored" else None
    return {
        "status": status,
        "s_size": len(system) if system is not None else None,
        "hits": hits,
        "precision": (hits / len(system) if system else 0.0) if hits is not None else None,
        "no_suitable_agreement": agreement if status == "no_suitable" else None,
    }


def measure(
    db: sqlite3.Connection,
    run_id: int,
    labels: list[Label],
    labels_b: list[Label] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    run = db.execute(
        "SELECT m.*,t.name AS template_name,t.version AS template_version,"
        "e.name AS embedding_name,e.pipeline_version AS embedding_version,"
        "e.file_sha256 AS embedding_sha FROM match_runs m "
        "JOIN scoring_templates t USING(template_id) "
        "LEFT JOIN embedding_models e USING(embedding_model_id) WHERE m.run_id=?",
        (run_id,),
    ).fetchone()
    if run is None or run["status"] != "completed":
        raise ValueError("run_id must identify a completed run")
    stamp = now if now is not None else datetime.now(UTC)
    if stamp.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    demand_ids = {int(key) for key in json.loads(run["dq_summary_json"] or "{}").get("demands", {})}
    results: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for source in db.execute(
        "SELECT m.*,COALESCE(m.candidate_id,r.candidate_id) AS profile_candidate_id "
        "FROM match_results m JOIN resources r USING(resource_id) "
        "WHERE m.run_id=? ORDER BY m.demand_id,m.rank,m.result_id",
        (run_id,),
    ):
        row = dict(source)
        results[row["demand_id"]].append(row)
        demand_ids.add(row["demand_id"])
    assessed = {
        row["code_requisition"]: row["demand_id"]
        for row in db.execute("SELECT demand_id,code_requisition FROM demands")
        if row["demand_id"] in demand_ids
    }
    emails: dict[str, set[Profile]] = defaultdict(set)
    for row in db.execute("SELECT resource_id,candidate_id,email_address FROM resources"):
        if row["email_address"]:
            emails[row["email_address"].casefold()].add(
                _profile(row["candidate_id"], row["resource_id"])
            )
    issues: list[dict[str, Any]] = []
    reference = _resolve(labels, emails, assessed, "A", issues)
    second = _resolve(labels_b, emails, assessed, "B", issues) if labels_b is not None else None
    overrides = active_overrides(db, run_id)
    demands: list[dict[str, Any]] = []
    variants = ["persisted", "after_overrides"] + (["analyst_b"] if second is not None else [])
    for code, gold in sorted(reference.items()):
        rows = results.get(assessed.get(code, -1), [])
        persisted: set[Profile] = set()
        effective: set[Profile] = set()
        persisted_suitable = effective_suitable = False
        for row in rows:
            profile = _profile(row["profile_candidate_id"], row["resource_id"])
            if 1 <= row["rank"] <= 5 and len(persisted) < 5:
                persisted.add(profile)
            persisted_suitable |= row["band"] in {"Strong", "Potential"}
            apply_overrides(row, overrides.get(row["result_id"], []))
            if not row["effective_excluded"]:
                effective_suitable |= row["effective_band"] in {"Strong", "Potential"}
                if row["effective_shortlisted"] and len(effective) < 5:
                    effective.add(profile)
        eligible = code in assessed and (bool(gold.profiles) or gold.no_suitable)
        comparisons = {
            "persisted": _comparison(persisted, gold, not persisted_suitable, eligible),
            "after_overrides": _comparison(effective, gold, not effective_suitable, eligible),
        }
        if second is not None:
            other = second.get(code)
            if other is None:
                issues.append({"analyst": "B", "requisition": code, "code": "missing_labels"})
            valid = other is not None and (bool(other.profiles) or other.no_suitable)
            comparisons["analyst_b"] = _comparison(
                set(other.profiles) if other is not None and valid else None,
                gold,
                other.no_suitable if other is not None else False,
                eligible,
            )
        demands.append(
            {
                "requisition": code,
                "split": gold.split,
                "g_size": len(gold.profiles),
                "variants": comparisons,
            }
        )
    splits: dict[str, Any] = {}
    for split in ("holdout", "tune"):
        split_rows = [row for row in demands if row["split"] == split]
        summary = {}
        for variant in variants:
            values = [row["variants"][variant] for row in split_rows]
            scored = [value for value in values if value["status"] == "scored"]
            no_suitable = [value for value in values if value["status"] == "no_suitable"]
            agree = sum(value["no_suitable_agreement"] for value in no_suitable)
            summary[variant] = {
                "demands_labelled": len(values),
                "scored": len(scored),
                "excluded": sum(value["status"] == "excluded" for value in values),
                "no_suitable": len(no_suitable),
                "no_suitable_agree": agree,
                "no_suitable_disagree": len(no_suitable) - agree,
                "hit_rate_at_5": sum(value["hits"] > 0 for value in scored) / len(scored)
                if scored
                else None,
                "precision_at_5": sum(value["precision"] for value in scored) / len(scored)
                if scored
                else None,
            }
        splits[split] = summary
    return {
        "run_id": run_id,
        "generated_at": stamp.astimezone(UTC).isoformat(),
        "primary_split": "holdout",
        "scoring_template": {"name": run["template_name"], "version": run["template_version"]},
        "llm_model": run["llm_model"],
        "embedding_model": {
            "name": run["embedding_name"],
            "version": run["embedding_version"],
            "sha": run["embedding_sha"],
        },
        "engine_version": run["engine_version"],
        "splits": splits,
        "demands": demands,
        "data_issues": issues,
    }


def markdown(report: dict[str, Any]) -> str:
    def cell(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")

    def table(headers: list[str], rows: list[list[Any]]) -> list[str]:
        return [
            "| " + " | ".join(cell(header) for header in headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
        ] + ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows]

    lines = [f"# KPI-01 · Run {report['run_id']}", "", "Primary split: **holdout**", ""]
    for key in (
        "generated_at",
        "scoring_template",
        "llm_model",
        "embedding_model",
        "engine_version",
    ):
        lines.append(f"- {key}: {cell(report[key])}")
    lines += ["", "## Metrics", ""]
    rows = []
    for split, variants in report["splits"].items():
        for variant, value in variants.items():
            rows.append(
                [
                    split,
                    variant.replace("_", " "),
                    value["demands_labelled"],
                    value["scored"],
                    value["excluded"],
                    value["hit_rate_at_5"],
                    value["precision_at_5"],
                    value["no_suitable_agree"],
                    value["no_suitable_disagree"],
                ]
            )
    lines += table(
        [
            "Split",
            "Variant",
            "Labelled",
            "Scored",
            "Excluded",
            "Hit-rate@5",
            "Precision@5",
            "No suitable: agree",
            "disagree",
        ],
        rows,
    )
    lines += ["", "## Per demand", ""]
    rows = []
    for demand in report["demands"]:
        for variant, value in demand["variants"].items():
            rows.append(
                [
                    demand["requisition"],
                    demand["split"],
                    variant.replace("_", " "),
                    value["status"],
                    value["s_size"],
                    demand["g_size"],
                    value["hits"],
                    value["precision"],
                    value["no_suitable_agreement"],
                ]
            )
    lines += table(
        [
            "Requisition",
            "Split",
            "Variant",
            "Status",
            "|S|",
            "|G|",
            "Hits",
            "Precision",
            "No suitable agreement",
        ],
        rows,
    )
    lines += ["", "## Data issues", ""]
    lines += table(
        ["Analyst", "Requisition", "Issue", "Line", "Masked email"],
        [
            [
                issue["analyst"],
                issue["requisition"],
                issue["code"],
                issue.get("line"),
                issue.get("email"),
            ]
            for issue in report["data_issues"]
        ],
    )
    return "\n".join(lines) + "\n"
