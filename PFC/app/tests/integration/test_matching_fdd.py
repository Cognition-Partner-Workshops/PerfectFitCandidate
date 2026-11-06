"""SQL-seeded FDD IDFNE323 and real semantic aggregation (REQ-MAT-01, REQ-SCR-04)."""

import math

import pytest
from test_matching_service import (
    FakeEmbedder,
    execute,
    seed_demand,
    seed_resource,
)
from test_matching_service import (
    runner as runner,
)
from test_matching_service import (
    settings as settings,
)

from pfc.config import Settings
from pfc.db.connection import database
from pfc.jobs.runner import JobRunner
from pfc.services.matching import MatchingPorts, demand_matches, excluded_view


class FddEmbedder(FakeEmbedder):
    dim = 6
    name = "fdd-geometry"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self.vector(text) for text in texts]

    def vector(self, text: str) -> list[float]:
        if text.startswith("Skill:"):
            term = text.split("\n")[0].removeprefix("Skill: ")
            axis = {"Azure": 1, "JIRA": 2, "Data Architect": 3}.get(term, 0)
            return [float(i == axis) for i in range(self.dim)]
        if text == "FDD JD":
            # Profile means are (3e0+e1)/sqrt(10), (2e0+e2)/sqrt(5),
            # and (3e0+e3)/sqrt(10). Construct their required JD cosines.
            a, b, c = (0.45 + 0.35 * value for value in (0.82, 0.55, 0.70))
            x = 0.715
            vector = [
                x,
                a * math.sqrt(10) - 3 * x,
                b * math.sqrt(5) - 2 * x,
                c * math.sqrt(10) - 3 * x,
                0.0,
                0.0,
            ]
            vector[4] = math.sqrt(1 - sum(v * v for v in vector))
            return vector
        partial_axis = {
            "Data engineering / data pipeline development": 3,
            "Python": 1,
            "Spark (or similar processing technology)": 3,
            "ETL/ELT pipelines and enterprise integration": 3,
            "Cloud data services, preferably AWS": 1,
            "Agile delivery": 2,
        }.get(text)
        vector = [0.0] * self.dim
        if partial_axis is not None:
            # 0.625 cosine normalises to 0.5 using the shipped thresholds.
            vector[partial_axis] = 0.625
            vector[5] = math.sqrt(1 - 0.625**2)
        else:
            vector[5] = 1.0
        return vector


def test_req_scr_01_fdd_idfne323_sql(settings: Settings, runner: JobRunner) -> None:
    with database(settings) as db:
        demand_id = seed_demand(
            db,
            "IDFNE323",
            jd="FDD JD",
            mandatory=(
                "Data engineering / data pipeline development",
                "Python",
                "SQL",
                "Spark (or similar processing technology)",
                "ETL/ELT pipelines and enterprise integration",
            ),
            preferred=(
                "Cloud data services, preferably AWS",
                "Agile delivery",
                "Structured and unstructured data at scale",
                "Data quality, reliability, security",
            ),
        )
        items = db.execute(
            "SELECT i.item_id FROM jd_requirement_items i JOIN jd_requirements q USING(jd_req_id) "
            "JOIN demand_versions v USING(demand_version_id) WHERE v.demand_id=? "
            "ORDER BY i.position",
            (demand_id,),
        ).fetchall()
        for item, term in zip(
            items,
            (
                "Data Engineer",
                "Python",
                "SQL",
                "Spark",
                "ETL/ELT",
                "AWS",
                "Agile",
                "Data at scale",
                "Data quality",
            ),
            strict=True,
        ):
            db.execute("INSERT OR IGNORE INTO skills(name,created_by) VALUES (?,'seed')", (term,))
            db.execute(
                "UPDATE jd_requirement_items SET canonical_skill_id="
                "(SELECT skill_id FROM skills WHERE name=?) WHERE item_id=?",
                (term, item[0]),
            )
        for name, grade, location, status, group, start, end, skills in [
            (
                "AlexB",
                "D1",
                "London",
                "Overall upskilling (New bench)",
                "RG1",
                None,
                None,
                "SQL,Python",
            ),
            (
                "AmitS",
                "C2",
                "London ",
                "Overall upskilling (New bench)",
                "RG1",
                "2026-08-24",
                None,
                "Data Engineer, SQL, Pyspark, Azure",
            ),
            (
                "RutujaK",
                "C1",
                "Liverpool",
                "Available",
                "RG4",
                "Wed 4/11/2026",
                None,
                "Python, SQL, JIRA",
            ),
            (
                "ShanthiM",
                "D2",
                "London",
                "CV with client - Demand Proposed",
                "RG3",
                None,
                None,
                "SQL,Python",
            ),
            (
                "JagmohanD",
                "C1",
                "London",
                "Selected",
                "Not on Bench",
                None,
                "2026-11-16",
                "SQL,Python",
            ),
            (
                "YesG",
                "C2",
                "Northampton",
                "CV with client - Proactive Proposed",
                "RG2",
                "2026-10-19",
                None,
                "Data Architect, SQL, DataBricks, Data Warehousing",
            ),
        ]:
            resource_id = seed_resource(
                db,
                name,
                grade=grade,
                location=location,
                status=status,
                group=group,
                end=end,
                end_status="valid" if end else "blank",
                skills=skills,
            )
            db.execute(
                "UPDATE resources SET bench_start_date=? WHERE resource_id=?", (start, resource_id)
            )
    run_id = execute(settings, runner, MatchingPorts(FddEmbedder()))
    with database(settings) as db:
        result = demand_matches(db, demand_id)
        rows = result["items"]
        assert [row["score"] for row in rows] == [63.1, 55.7, 51.3]
        assert [row["band"] for row in rows] == ["Avg", "Weak", "Weak"]
        assert [row["potential_person"] for row in rows if row["shortlisted"]] == [
            "AmitS",
            "RutujaK",
            "YesG",
        ]
        assert not result["no_suitable_candidate"] and not result["weak_only"]
        skills = [
            row[0]
            for row in db.execute(
                "SELECT b.points FROM score_breakdowns b JOIN match_results m USING(result_id)"
                " WHERE m.run_id=? AND b.component='skills' ORDER BY m.rank",
                (run_id,),
            )
        ]
        assert skills == pytest.approx([45.0625, 27.7375, 33.25])
        reasons = {(row["short_name"], row["reason_code"]) for row in excluded_view(db, run_id)}
        assert reasons == {
            ("AlexB", "GRADE_GAP_GT1"),
            ("ShanthiM", "GRADE_GAP_GT1"),
            ("JagmohanD", "STATUS_EXCLUDED"),
            ("JagmohanD", "NOT_ON_BENCH"),
            ("JagmohanD", "BENCH_END_SET"),
        }
