"""Demand/person filters are independent; match filters use scored display facts."""

import json
import sqlite3
from collections import Counter
from datetime import date
from typing import Any

from pfc.api.schemas.analytics import DashboardFilters
from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise import normalise_term, tokenise_skills
from pfc.security.problems import Problem
from pfc.services import matching
from pfc.services.matching.results import _mask_email

BANDS = ("Strong", "Potential", "Avg", "Weak", "Insufficient data")


def equal(actual: Any, expected: str | None) -> bool:
    return not expected or str(actual or "").casefold() == expected.casefold()


def contains(actual: Any, expected: str | None) -> bool:
    return not expected or expected.casefold() in str(actual or "").casefold()


def counts(values: list[str], limit: int | None = None) -> list[dict[str, Any]]:
    rows = sorted(Counter(values).items(), key=lambda item: (-item[1], item[0].casefold(), item[0]))
    return [{"label": label, "count": count} for label, count in rows[:limit]]


class Population:
    def __init__(self, db: sqlite3.Connection, filters: DashboardFilters, today: date):
        self.db, self.filters, self.today = db, filters, today
        self.config = load_domain_config()
        self.grades = (
            tuple(row[0] for row in db.execute("SELECT code FROM grades ORDER BY ordinal"))
            or self.config.grades.codes
        )
        if filters.run_id is None:
            latest = matching.list_results(db, limit=1)["run_id"]
            self.run = matching.get_run(db, latest) if latest is not None else None
        else:
            self.run = matching.get_run(db, filters.run_id)
            if self.run is None:
                raise Problem(404, "RUN_NOT_FOUND", "Match run not found")
        self.results = (
            []
            if self.run is None
            else [
                row
                for row in matching.list_results(db, self.run["run_id"], limit=None)["items"]
                if not row["effective_excluded"] and self.match_filter(row)
            ]
        )
        self.demands = self.load_demands()
        self.resources = self.load_resources()

    def demand_filter(self, row: dict[str, Any]) -> bool:
        f = self.filters
        return (
            contains(row["code_requisition"], f.code_requisition)
            and equal(row["account_name"], f.account_name)
            and equal(row["bu"], f.bu)
            and equal(row["region"], f.region)
            and equal(row["code_local_grade"], f.grade)
        )

    def person_filter(self, row: dict[str, Any]) -> bool:
        f = self.filters
        return (
            equal(row["grade"], f.grade)
            and equal(row["location"], f.location)
            and equal(row["bench_status"], f.status)
            and equal(row["candidate_type"], f.candidate_type)
            and (
                not f.incomplete_identity
                or (row["identity_status"] != "complete") == (f.incomplete_identity == "yes")
            )
        )

    def match_filter(self, row: dict[str, Any]) -> bool:
        f = self.filters
        return (
            contains(row["requisition"], f.code_requisition)
            and equal(row["account"], f.account_name)
            and equal(row["bu"], f.bu)
            and equal(row["role_location"], f.region)
            and (
                not f.grade
                or equal(row["demand_grade"], f.grade)
                or equal(row["person_grade"], f.grade)
            )
            and equal(row["person_location"], f.location)
            and equal(row["status"], f.status)
            and equal(row["effective_band"], f.band)
            and equal(row["candidate_type"], f.candidate_type)
            and (
                not f.incomplete_identity
                or bool(row["incomplete_identity"]) == (f.incomplete_identity == "yes")
            )
            and (
                not f.mandatory_skills
                or (row["mandatory_missing"] == 0) == (f.mandatory_skills == "met")
            )
            and (f.score_min is None or row["score"] >= f.score_min)
            and (f.score_max is None or row["score"] <= f.score_max)
        )

    def load_demands(self) -> list[dict[str, Any]]:
        rows = []
        for source in self.db.execute(
            "SELECT d.*,v.jd_text_norm FROM demands d LEFT JOIN demand_versions v "
            "ON v.demand_version_id=d.current_version_id WHERE d.lifecycle='open' "
            "ORDER BY d.position_start_date IS NULL,d.position_start_date DESC,d.demand_id"
        ):
            row = dict(source)
            if not self.demand_filter(row):
                continue
            req = self.db.execute(
                "SELECT * FROM jd_requirements WHERE demand_version_id=? "
                "AND status='confirmed' ORDER BY revision DESC LIMIT 1",
                (row["current_version_id"],),
            ).fetchone()
            row["mandatory_skills"], row["preferred_skills"] = [], []
            if req:
                for item in self.db.execute(
                    "SELECT i.kind,i.text,s.name FROM jd_requirement_items i LEFT JOIN skills s "
                    "ON s.skill_id=i.canonical_skill_id WHERE i.jd_req_id=? "
                    "AND i.kind IN ('mandatory_skill','preferred_skill') "
                    "ORDER BY i.position,i.item_id",
                    (req["jd_req_id"],),
                ):
                    canonical = (
                        item["name"] or self.config.synonyms.resolve(item["text"]) or item["text"]
                    )
                    row[item["kind"] + "s"].append(canonical)
            latest = self.db.execute(
                "SELECT status FROM jd_requirements WHERE demand_version_id=? "
                "AND status IN ('draft','confirmed') ORDER BY revision DESC LIMIT 1",
                (row["current_version_id"],),
            ).fetchone()
            matches = [r for r in self.results if r["demand_id"] == row["demand_id"]]
            status = (
                "Not analysed"
                if latest is None
                else (
                    "Analysed — awaiting confirmation"
                    if latest["status"] == "draft"
                    else "Confirmed — scoring"
                )
            )
            if (
                latest is None
                and self.db.execute(
                    "SELECT 1 FROM jd_requirements q JOIN demand_versions v "
                    "ON v.demand_version_id=q.demand_version_id "
                    "WHERE v.demand_id=? AND v.demand_version_id!=? LIMIT 1",
                    (row["demand_id"], row["current_version_id"]),
                ).fetchone()
            ):
                status = "JD changed"
            assessed = json.loads((self.run or {}).get("dq_summary_json") or "{}").get(
                "demands", {}
            )
            if latest and latest["status"] == "confirmed" and str(row["demand_id"]) in assessed:
                status = "Scored"
            row.update(
                jd_status=status,
                matches=len(matches),
                best_band=min(
                    (r["effective_band"] for r in matches), key=BANDS.index, default=None
                ),
            )
            rows.append(row)
        return rows

    def load_resources(self) -> list[dict[str, Any]]:
        rows = []
        for source in self.db.execute(
            "SELECT r.*,c.display_name,COALESCE(c.identity_status,'incomplete') AS identity_status,"
            "COALESCE(c.classification_override,c.classification,'Unknown') AS candidate_type "
            "FROM resources r LEFT JOIN candidates c USING(candidate_id) "
            "WHERE r.lifecycle='active' "
            "ORDER BY r.bench_start_date IS NULL,r.bench_start_date DESC,r.resource_id"
        ):
            row = dict(source)
            if not self.person_filter(row):
                continue
            tokens = tokenise_skills(row["skills_raw"] or "", self.config.synonyms)
            row["skills"] = [token.canonical or token.raw for token in tokens.tokens]
            row["cv_count"] = self.db.execute(
                "SELECT COUNT(*) FROM cv_versions WHERE candidate_id=?", (row["candidate_id"],)
            ).fetchone()[0]
            status = self.config.bench_statuses.resolve(row["bench_status"] or "")
            mapped = self.db.execute(
                "SELECT priority,is_excluded FROM status_map WHERE source_value=? COLLATE NOCASE",
                (normalise_term(row["bench_status"] or ""),),
            ).fetchone()
            row["status_priority"] = (
                mapped["priority"] if mapped else status.priority if status else 2
            )
            row["status_excluded"] = bool(
                mapped["is_excluded"] if mapped else status and status.is_excluded
            )
            row["unmapped_status"] = mapped is None and status is None
            rows.append(row)
        return rows

    def demand_rows(self) -> list[dict[str, Any]]:
        fields = (
            "demand_id",
            "code_requisition",
            "id_format_ok",
            "position_start_date",
            "account_name",
            "code_local_grade",
            "demand_request_name",
            "region",
            "bu",
            "owner",
            "jd_status",
            "matches",
            "best_band",
            "mandatory_skills",
            "preferred_skills",
        )
        return [
            {
                **{key: row[key] for key in fields},
                "jd_text": row["jd_text_norm"] or "",
                "uploaded_at": row["created_at"],
            }
            for row in self.demands
        ]

    def resource_rows(self) -> list[dict[str, Any]]:
        fields = (
            "resource_id",
            "short_name",
            "grade",
            "location",
            "bench_status",
            "unmapped_status",
            "skills",
            "bench_start_date",
            "bench_end_date",
            "group_name",
            "group_owner",
            "eligibility",
            "cv_count",
            "candidate_type",
            "identity_status",
        )
        return [
            {
                **{key: row[key] for key in fields},
                "candidate_id": row["candidate_id"] or 0,
                "email_address_masked": _mask_email(row["email_address"]),
                "name": row["display_name"],
                "bench_status_priority": row["status_priority"],
                "available_capacity": capacity(row["available_capacity"]),
                "exclusion_reasons": [
                    term for term in (row["exclusion_reason"] or "").split(",") if term
                ],
                "cv_status": "Synced" if row["cv_count"] else "Not synced",
            }
            for row in self.resources
        ]


def capacity(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return "Y" if value.strip().casefold() in {"y", "yes"} else "N"
