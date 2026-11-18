"""Extended dashboard projections; metric definitions live in README.md."""

import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any

from pfc.services.pii import mask_email, mask_emails_in

from .dashboard import percent, top_demand_skills
from .population import Population, counts


def timestamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value or "")
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def person(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_id": row["resource_id"],
        "candidate_id": row["candidate_id"] or 0,
        "name": mask_emails_in(row["display_name"] or row["short_name"] or ""),
        "email_masked": mask_email(row["email_address"]),
    }


def cells(counter: Counter[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {"row": mask_emails_in(a), "column": mask_emails_in(b), "count": n}
        for (a, b), n in sorted(counter.items())
    ]


def result_rows(p: Population) -> list[dict[str, Any]]:
    rows = {r["result_id"]: r for r in p._match_rows(include_excluded=True)}
    if not rows:
        return []
    placeholders = ",".join("?" for _ in p.runs)
    for source in p.db.execute(
        "SELECT result_id,run_id,rank,shortlisted FROM match_results "
        f"WHERE run_id IN ({placeholders})",
        tuple(p.runs),
    ):
        if (row := rows.get(source["result_id"])) is not None:
            row.update(dict(source))
    return sorted(
        rows.values(), key=lambda r: (r["run_id"], r["demand_id"], r["rank"], r["result_id"])
    )


def email_trend(p: Population) -> list[dict[str, Any]]:
    weeks: dict[str, list[float]] = defaultdict(list)
    for row in p.email_rows:
        sent = timestamp(row["sent_at"]) if row["status"] == "Sent" else None
        run = p.runs.get(p.assessed_run_ids.get(row["demand_id"], -1), {})
        start = timestamp(row["uploaded_at"]) or timestamp(run.get("started_at"))
        if sent is None or start is None or sent < start:
            continue
        week = (sent.date() - timedelta(days=sent.weekday())).isoformat()
        weeks[week].append((sent - start).total_seconds() / 3600)
    return [
        {
            "week": week,
            "sent_count": len(hours),
            "median_hours": round(median(hours), 2),
            "within24hPct": percent(sum(h <= 24 for h in hours), len(hours)),
        }
        for week, hours in sorted(weeks.items())
    ]


def review_metrics(p: Population, rows: list[dict[str, Any]]) -> dict[str, Any]:
    approved = {
        (e["demand_id"], e["resource_id"])
        for e in p.email_rows
        if e["approved_at"] is not None or e["status"] in {"Approved", "Sending", "Sent"}
    }
    sent = {
        (e["demand_id"], e["resource_id"])
        for e in p.email_rows
        if e["status"] == "Sent" and e["sent_at"] is not None
    }
    confirmed: set[int] = set()
    by_run: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_run[row["run_id"]].append(row)
        if row["effective_excluded"]:
            continue
        actions = p.overrides.get(row["result_id"], [])
        shortlist_action = next(
            (o["action"] for o in reversed(actions) if o["action"] in {"pin", "include", "unpin"}),
            None,
        )
        if (
            shortlist_action in {"pin", "include"}
            or (row["demand_id"], row["resource_id"]) in approved
        ):
            confirmed.add(row["result_id"])
    visible = [r for r in rows if not r["effective_excluded"]]
    matched = {r["demand_id"] for r in visible}
    reviewed = {r["demand_id"] for r in rows if r["result_id"] in confirmed}
    approved_demands = {
        r["demand_id"] for r in visible if (r["demand_id"], r["resource_id"]) in approved
    }
    sent_demands = {r["demand_id"] for r in visible if (r["demand_id"], r["resource_id"]) in sent}
    accuracy = []
    overrides = []
    for run_id, results in sorted(by_run.items()):
        actual = [r for r in results if r["result_id"] in confirmed]
        demand_ids = {r["demand_id"] for r in actual}
        top = [
            r
            for r in results
            if not r["effective_excluded"] and r["demand_id"] in demand_ids and r["rank"] <= 5
        ]
        hits = [r for r in top if r["result_id"] in confirmed]
        accuracy.append(
            {
                "run_id": run_id,
                "confirmed_demands": len(demand_ids),
                "confirmed_pairs": len(actual),
                "hits": len(hits),
                "hitRateAt5": round(len({r["demand_id"] for r in hits}) / len(demand_ids), 4)
                if demand_ids
                else None,
                "precisionAt5": round(len(hits) / len(top), 4) if top else None,
            }
        )
        override_actions = [
            o["action"] for r in results for o in p.overrides.get(r["result_id"], [])
        ]
        overridden = sum(bool(p.overrides.get(r["result_id"])) for r in results)
        overrides.append(
            {
                "run_id": run_id,
                "pair_count": len(results),
                "overridden_pairs": overridden,
                "rate_pct": percent(overridden, len(results)),
                "by_type": counts(override_actions),
            }
        )
    return {
        "shortlist_funnel": [
            {"label": label, "count": len(ids)}
            for label, ids in (
                ("Matched", matched),
                ("Reviewed / shortlisted", reviewed),
                ("Email approved", approved_demands),
                ("Email sent", sent_demands),
            )
        ],
        "accuracy": accuracy,
        "override_rates": overrides,
    }


def build(p: Population) -> dict[str, Any]:
    scoped_rows = result_rows(p)
    rows = [r for r in scoped_rows if not r["effective_excluded"]]
    eligible = [r for r in p.resources if r["eligibility"] == "eligible"]
    resources = {r["resource_id"]: r for r in p.resources}
    demands = {d["demand_id"]: d for d in p.demands}
    by_demand: dict[int, list[dict[str, Any]]] = defaultdict(list)
    best: dict[int, float] = {}
    contention: dict[int, set[int]] = defaultdict(set)
    histogram = [
        {"lower": low, "upper": low + 5, "all_pairs": 0, "top5": 0} for low in range(0, 100, 5)
    ]
    for row in rows:
        by_demand[row["demand_id"]].append(row)
        rid = row["resource_id"]
        best[rid] = max(best.get(rid, 0), row["score"])
        bucket = histogram[min(19, max(0, int(row["score"] // 5)))]
        bucket["all_pairs"] += 1
        bucket["top5"] += int(row["rank"] <= 5)
        if row["effective_band"] in {"Strong", "Potential"} and row["demand_id"] in demands:
            contention[rid].add(row["demand_id"])

    demand_grades = Counter(d["code_local_grade"] for d in p.demands)
    bench_grades = Counter(r["grade"] for r in eligible)
    grade_matrix = [
        {
            "row": dg,
            "column": bg,
            "count": demand_grades[dg] * bench_grades[bg],
            "demand_count": demand_grades[dg],
            "bench_count": bench_grades[bg],
            "relation": "exact" if i == j else "adjacent" if abs(i - j) == 1 else "outside",
        }
        for i, dg in enumerate(p.grades)
        for j, bg in enumerate(p.grades)
    ]
    unmatched = []
    excluded_demands = {r["demand_id"] for r in scoped_rows if r["effective_excluded"]}
    for demand in p.demands:
        matches = by_demand[demand["demand_id"]]
        if any(r["effective_band"] in {"Strong", "Potential", "Avg"} for r in matches):
            continue
        grade = demand["code_local_grade"]
        window = [
            r
            for r in eligible
            if grade in p.grades
            and r["grade"] in p.grades
            and abs(p.grades.index(grade) - p.grades.index(r["grade"])) <= 1
        ]
        reasons = []
        if (p.resources and not eligible) or (
            not matches and demand["demand_id"] in excluded_demands
        ):
            reasons.append("all_candidates_excluded")
        elif grade not in p.grades:
            reasons.append("insufficient_data")
        elif not window:
            reasons.append("no_person_in_grade_window")
        if any(r["mandatory_missing"] > 0 for r in matches):
            reasons.append("mandatory_skill_gap")
        if any(r["effective_band"] == "Insufficient data" for r in matches):
            reasons.append("insufficient_data")
        unmatched.append(
            {
                "demand_id": demand["demand_id"],
                "code_requisition": demand["code_requisition"],
                "position_start_date": demand["position_start_date"],
                "reason_codes": reasons or ["other"],
            }
        )
    unmatched.sort(
        key=lambda d: (d["position_start_date"] or "9999", d["code_requisition"], d["demand_id"])
    )

    readiness = Counter(
        (r["group_name"] or "Not recorded", r["bench_status"] or "Not recorded")
        for r in p.resources
    )
    regions = Counter(d["region"] or "Unspecified" for d in p.demands)
    locations: Counter[str] = Counter()
    for resource in eligible:
        locations.update(
            {
                s.strip()
                for s in re.split(r"[/;,|]", resource["location"] or "Unspecified")
                if s.strip()
            }
        )
    location_matrix = Counter(
        {
            (region, location): n * m
            for region, n in regions.items()
            for location, m in locations.items()
        }
    )
    skill_sets = [set(d["mandatory_skills"] + d["preferred_skills"]) for d in p.demands]
    skill_counts = Counter(s for skills in skill_sets for s in skills)
    skills = sorted(skill_counts, key=lambda s: (-skill_counts[s], s.casefold(), s))[:15]
    cooccurrence = [
        {
            "row": mask_emails_in(a),
            "column": mask_emails_in(b),
            "count": sum(a in s and b in s for s in skill_sets),
        }
        for a in skills
        for b in skills
    ]
    demanded = [s["label"] for s in top_demand_skills(p)]
    upskilling = []
    for resource in p.resources:
        if (resource["bench_status"] or "").strip().casefold() != "overall upskilling (new bench)":
            continue
        owned = {s.casefold() for s in resource["skills"]}
        upskilling.append(
            {
                **person(resource),
                "matched_skills": [mask_emails_in(s) for s in demanded if s.casefold() in owned],
                "missing_skills": [
                    mask_emails_in(s) for s in demanded if s.casefold() not in owned
                ],
                "best_score": best.get(resource["resource_id"]),
            }
        )
    upskilling.sort(
        key=lambda r: (-(r["best_score"] if r["best_score"] is not None else -1), r["resource_id"])
    )

    age_counts = [0] * 5
    unknown = 0
    uploads = {
        r["ingestion_run_id"]: r["started_at"]
        for r in p.db.execute("SELECT ingestion_run_id,started_at FROM ingestion_runs")
    }
    for demand in p.demands:
        first = timestamp(uploads.get(demand["first_ingestion_id"])) or timestamp(
            demand["created_at"]
        )
        if first is None:
            unknown += 1
            continue
        days = max(0, (p.today - first.date()).days)
        age_counts[
            next(i for i, limit in enumerate((7, 14, 30, 60, float("inf"))) if days < limit)
        ] += 1
    overdue = 0
    for demand in unmatched:
        try:
            overdue += date.fromisoformat(demand["position_start_date"] or "") < p.today
        except ValueError:
            pass
    exclusions = [
        mask_emails_in(reason.strip())
        for r in p.resources
        if r["eligibility"] != "eligible"
        for reason in sorted(set((r["exclusion_reason"] or "other").split(",")))
        if reason.strip()
    ]
    contended = sorted(
        ((rid, ids) for rid, ids in contention.items() if len(ids) > 1 and rid in resources),
        key=lambda item: (-len(item[1]), item[0]),
    )[:20]
    return {
        "grade_matrix": grade_matrix,
        "score_histogram": {"thresholds": [61, 70, 90], "bins": histogram},
        **review_metrics(p, scoped_rows),
        "unmatched_demands": unmatched,
        "email_trend": email_trend(p),
        "readiness_status": cells(readiness),
        "location_matrix": cells(location_matrix),
        "skill_cooccurrence": cooccurrence,
        "upskilling": upskilling,
        "exclusion_reasons": counts(exclusions),
        "candidate_types": [
            {"label": label, "count": sum(r["candidate_type"] == label for r in p.resources)}
            for label in ("Internal", "External", "Unknown")
        ],
        "contention": [
            {
                **person(resources[rid]),
                "demand_count": len(ids),
                "requisitions": sorted(demands[d]["code_requisition"] for d in ids),
            }
            for rid, ids in contended
        ],
        "demand_aging": {
            "buckets": [
                {"label": label, "count": n}
                for label, n in zip(
                    ("0–6 days", "7–13 days", "14–29 days", "30–59 days", "60+ days"),
                    age_counts,
                    strict=True,
                )
            ],
            "unknown_start": unknown,
            "past_start_without_match": overdue,
        },
    }
