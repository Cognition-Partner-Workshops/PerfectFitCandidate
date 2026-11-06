"""Demand/person filters are independent; match filters use scored display facts."""

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import date
from functools import cached_property
from typing import Any

from pfc.api.schemas.analytics import DashboardFilters
from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise import normalise_term, tokenise_skills
from pfc.security.problems import Problem
from pfc.services import matching
from pfc.services.matching import results as result_views
from pfc.services.matching.lifecycle import reconcile_runs
from pfc.services.matching.results import _mask_email, active_overrides, apply_overrides

BANDS = ("Strong", "Potential", "Avg", "Weak", "Insufficient data")


def _capacity_input(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            "capacity" in str(key).casefold() or _capacity_input(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(_capacity_input(item) for item in value)


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
            reconcile_runs(db)
            source = db.execute(
                "SELECT run_id FROM match_runs WHERE status='completed' "
                "ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            latest = source[0] if source else None
            self.run = matching.get_run(db, latest) if latest is not None else None
        else:
            self.run = matching.get_run(db, filters.run_id)
            if self.run is None:
                raise Problem(404, "RUN_NOT_FOUND", "Match run not found")

    @cached_property
    def overrides(self) -> dict[int, list[dict[str, Any]]]:
        return active_overrides(self.db, self.run["run_id"]) if self.run else {}

    @cached_property
    def has_match_filters(self) -> bool:
        return any(
            value is not None and value != ""
            for value in self.filters.model_dump(exclude={"run_id"}).values()
        )

    @cached_property
    def filtered_results(self) -> list[dict[str, Any]]:
        return list(self._match_rows())

    @cached_property
    def result_counts(self) -> Counter[tuple[int, str]]:
        if self.run is None:
            return Counter()
        if self.has_match_filters:
            return Counter((r["demand_id"], r["effective_band"]) for r in self.filtered_results)
        counts = Counter(
            {
                (row["demand_id"], row["band"]): row["n"]
                for row in self.db.execute(
                    "SELECT demand_id,band,COUNT(*) AS n FROM match_results "
                    "WHERE run_id=? GROUP BY demand_id,band",
                    (self.run["run_id"],),
                )
            }
        )
        if self.overrides:
            for source in self.db.execute(
                "SELECT result_id,demand_id,band FROM match_results WHERE run_id=? "
                "AND result_id IN (SELECT result_id FROM overrides WHERE revoked_at IS NULL)",
                (self.run["run_id"],),
            ):
                row = dict(source)
                counts[(row["demand_id"], row["band"])] -= 1
                apply_overrides(row, self.overrides[row["result_id"]])
                if not row["effective_excluded"]:
                    counts[(row["demand_id"], row["effective_band"])] += 1
        return +counts

    @staticmethod
    def _snapshot(key: str, fallback: str) -> str:
        path = f"$.display_facts.{key}"
        return (
            f"CASE WHEN json_type(b.inputs_json,'{path}') IS NOT NULL "
            f"THEN json_extract(b.inputs_json,'{path}') ELSE {fallback} END"
        )

    def _match_rows(self, *, validation: bool = False) -> Iterator[dict[str, Any]]:
        if self.run is None:
            return
        fields = {
            "requisition": ("code_requisition", "d.code_requisition"),
            "account": ("account_name", "d.account_name"),
            "bu": ("bu", "d.bu"),
            "role_location": ("region", "COALESCE(NULLIF(d.region,''),'Unspecified')"),
            "demand_grade": ("grade", "d.code_local_grade"),
            "person_grade": ("grade", "r.grade"),
            "person_location": ("location", "r.location"),
            "status": ("status", "r.bench_status"),
        }
        columns = [
            "m.result_id",
            "m.demand_id",
            "m.resource_id",
            "m.score",
            "m.band",
            "m.mandatory_missing",
        ]
        selected = (
            {
                key: self._snapshot(key, fallback)
                for key, (attribute, fallback) in fields.items()
                if getattr(self.filters, attribute)
            }
            if not validation
            else {}
        )
        columns += [f"{expression} AS {key}" for key, expression in selected.items()]
        if not validation:
            columns += [
                "COALESCE(c.classification_override,c.classification,'Unknown') AS candidate_type",
                "COALESCE(c.identity_status!='complete',0) AS incomplete_identity",
            ]
        else:
            columns += [
                "m.matching_evidence",
                "m.gaps_checks",
                "r.email_address",
                "json_extract(b.inputs_json,'$.display_facts.person_email') AS email_snapshot",
                "json_type(b.inputs_json,'$.display_facts.person_email') IS NOT NULL "
                "AS has_email_snapshot",
                self._snapshot("start_date", "d.position_start_date") + " AS start_date",
                "json_extract(g.inputs_json,'$.distance') AS grade_distance",
                "json_extract(e.inputs_json,'$.parse_status') AS end_status",
            ]
        joins = (
            " FROM match_results m INDEXED BY ix_results_demand JOIN demands d USING(demand_id) "
            "JOIN resources r USING(resource_id) "
        )
        if selected or validation:
            joins += (
                "LEFT JOIN score_breakdowns b ON b.result_id=m.result_id AND b.component='skills' "
            )
        if not validation:
            joins += "LEFT JOIN candidates c ON c.candidate_id=m.candidate_id "
        else:
            joins += (
                "LEFT JOIN score_breakdowns g ON g.result_id=m.result_id AND g.component='grade' "
                "LEFT JOIN score_breakdowns e ON e.result_id=m.result_id "
                "AND e.component='bench_end' "
            )
        included = (
            {r["result_id"] for r in self.filtered_results}
            if validation and self.has_match_filters
            else None
        )
        for source in self.db.execute(
            "SELECT " + ",".join(columns) + joins + " WHERE m.run_id=?", (self.run["run_id"],)
        ):
            row = dict(source)
            if validation:
                row["person_email"] = result_views._mask_email(
                    row["email_snapshot"] if row["has_email_snapshot"] else row["email_address"]
                )
            if included is not None:
                if row["result_id"] in included:
                    yield row
                continue
            if not validation or self.overrides:
                apply_overrides(row, self.overrides.get(row["result_id"], []))
                if row["effective_excluded"]:
                    continue
            if self.has_match_filters and not validation:
                row = {**dict.fromkeys(fields), **row}
                if not self.match_filter(row):
                    continue
            yield row

    @cached_property
    def validation_rows(self) -> list[dict[str, Any]]:
        return list(self._match_rows(validation=True))

    @cached_property
    def capacity_result_ids(self) -> set[int]:
        """REQ-OUT-10: inspect suspect scoring inputs once per run, not per result."""
        if self.run is None:
            return set()
        return {
            row["result_id"]
            for row in self.db.execute(
                "SELECT b.result_id,b.inputs_json FROM score_breakdowns b "
                "JOIN match_results m USING(result_id) "
                "WHERE m.run_id=? AND b.inputs_json LIKE '%capacity%'",
                (self.run["run_id"],),
            )
            if _capacity_input(json.loads(row["inputs_json"]))
        }

    @cached_property
    def demands(self) -> list[dict[str, Any]]:
        return self.load_demands()

    @cached_property
    def resources(self) -> list[dict[str, Any]]:
        return self.load_resources()

    @cached_property
    def all_demands(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM demands")]

    @cached_property
    def email_rows(self) -> list[dict[str, Any]]:
        demand_ids = {row["demand_id"] for row in self.all_demands if self.demand_filter(row)}
        resource_ids = {
            row["resource_id"]
            for row in self.db.execute(
                "SELECT r.*,COALESCE(c.identity_status,'incomplete') AS identity_status,"
                "COALESCE(c.classification_override,c.classification,'Unknown') AS candidate_type "
                "FROM resources r LEFT JOIN candidates c USING(candidate_id)"
            )
            if self.person_filter(dict(row))
        }
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT e.*,i.started_at AS uploaded_at FROM email_messages e "
                "JOIN demands d USING(demand_id) LEFT JOIN ingestion_runs i "
                "ON i.ingestion_run_id=d.first_ingestion_id"
            )
            if row["demand_id"] in demand_ids and row["resource_id"] in resource_ids
        ]

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
        confirmed: dict[int, sqlite3.Row] = {}
        latest: dict[int, sqlite3.Row] = {}
        for req in self.db.execute(
            "SELECT q.* FROM jd_requirements q JOIN demands d "
            "ON d.current_version_id=q.demand_version_id WHERE d.lifecycle='open' "
            "ORDER BY q.revision DESC"
        ):
            version = req["demand_version_id"]
            if req["status"] == "confirmed":
                confirmed.setdefault(version, req)
            if req["status"] in {"draft", "confirmed"}:
                latest.setdefault(version, req)
        items: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for item in self.db.execute(
            "SELECT i.*,s.name FROM jd_requirement_items i "
            "JOIN jd_requirements q USING(jd_req_id) "
            "JOIN demands d ON d.current_version_id=q.demand_version_id "
            "LEFT JOIN skills s ON s.skill_id=i.canonical_skill_id "
            "WHERE d.lifecycle='open' AND q.status='confirmed' "
            "AND i.kind IN ('mandatory_skill','preferred_skill') ORDER BY i.position,i.item_id"
        ):
            items[item["jd_req_id"]].append(item)
        historical = {
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT v.demand_id FROM jd_requirements q "
                "JOIN demand_versions v USING(demand_version_id) JOIN demands d USING(demand_id) "
                "WHERE v.demand_version_id!=d.current_version_id"
            )
        }
        by_demand: dict[int, Counter[str]] = defaultdict(Counter)
        for (demand_id, band), count in self.result_counts.items():
            by_demand[demand_id][band] += count
        assessed = json.loads((self.run or {}).get("dq_summary_json") or "{}").get("demands", {})
        for source in self.db.execute(
            "SELECT d.*,v.jd_text_norm FROM demands d LEFT JOIN demand_versions v "
            "ON v.demand_version_id=d.current_version_id WHERE d.lifecycle='open' "
            "ORDER BY d.position_start_date IS NULL,d.position_start_date DESC,d.demand_id"
        ):
            row = dict(source)
            if not self.demand_filter(row):
                continue
            req = confirmed.get(row["current_version_id"])
            row["mandatory_skills"], row["preferred_skills"] = [], []
            if req:
                for item in items[req["jd_req_id"]]:
                    canonical = (
                        item["name"] or self.config.synonyms.resolve(item["text"]) or item["text"]
                    )
                    row[item["kind"] + "s"].append(canonical)
            current = latest.get(row["current_version_id"])
            matches = by_demand[row["demand_id"]]
            status = (
                "Not analysed"
                if current is None
                else (
                    "Analysed — awaiting confirmation"
                    if current["status"] == "draft"
                    else "Confirmed — scoring"
                )
            )
            if current is None and row["demand_id"] in historical:
                status = "JD changed"
            if current and current["status"] == "confirmed" and str(row["demand_id"]) in assessed:
                status = "Scored"
            row.update(
                jd_status=status,
                matches=sum(matches.values()),
                best_band=min(matches, key=BANDS.index, default=None),
            )
            rows.append(row)
        return rows

    def load_resources(self) -> list[dict[str, Any]]:
        rows = []
        cv_counts = dict(
            self.db.execute("SELECT candidate_id,COUNT(*) FROM cv_versions GROUP BY candidate_id")
        )
        ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        statuses: dict[str, sqlite3.Row] = {}
        for mapped in self.db.execute("SELECT * FROM status_map"):
            statuses.setdefault(mapped["source_value"].translate(ascii_lower), mapped)
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
            row["cv_count"] = cv_counts.get(row["candidate_id"], 0)
            status = self.config.bench_statuses.resolve(row["bench_status"] or "")
            mapped = statuses.get(normalise_term(row["bench_status"] or "").translate(ascii_lower))
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
