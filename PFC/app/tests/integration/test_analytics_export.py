"""Production dashboard/export routes with SQL fixtures and real audit chains."""

import csv
import io
import json
from datetime import timedelta
from typing import Any

import pytest
from openpyxl import load_workbook
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import NOW, seed_demand, seed_resource

from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.services.matching.export import DISCLAIMER, HEADERS

PREFIX = "/api/v1"
PANELS = ("summary", "demand", "bench", "match")


@pytest.fixture
def populated(api: Api) -> tuple[Api, dict[str, Any]]:
    client, _ = api
    settings = client.app.state.settings
    stamp = NOW.isoformat()
    with database(settings) as db:
        ingestions = []
        for kind in ("DL", "DL", "CT"):
            cursor = db.execute(
                "INSERT INTO ingestion_runs(kind,source,file_name,file_sha256,file_size,"
                "status,started_at,committed_at) "
                "VALUES (?,'manual_upload','data.xlsx',?,1,'committed',?,?)",
                (kind, str(len(ingestions)), stamp, stamp),
            )
            ingestions.append(cursor.lastrowid)
        for ingestion, severity in (
            (ingestions[0], "warning"),
            (ingestions[0], "error"),
            (ingestions[1], "warning"),
            (ingestions[1], "info"),
            (ingestions[2], "error"),
        ):
            db.execute(
                "INSERT INTO ingestion_row_issues"
                "(ingestion_run_id,row_number,severity,code,message) "
                "VALUES (?,1,?,'TEST','issue')",
                (ingestion, severity),
            )
        demands = [seed_demand(db, f"T1{i:04d}", preferred=("SQL", "Python")) for i in range(4)]
        db.execute("UPDATE demands SET lifecycle='closed' WHERE demand_id=?", (demands[3],))
        db.execute("UPDATE demands SET first_ingestion_id=?,bu='Data'", (ingestions[1],))
        db.execute(
            "UPDATE demands SET position_start_date=? WHERE demand_id=?",
            ((NOW.date() - timedelta(days=1)).isoformat(), demands[0]),
        )
        resources = [
            seed_resource(db, f"Person{i}", email=f"person{i}@example.com") for i in range(4)
        ]
        db.execute("UPDATE resources SET eligibility='eligible'")
        db.execute(
            "UPDATE resources SET eligibility='excluded',bench_status='Selected' "
            "WHERE resource_id=?",
            (resources[3],),
        )
        for resource, capacity in zip(resources[:3], (" Yes ", "N", None), strict=True):
            db.execute(
                "UPDATE resources SET available_capacity=? WHERE resource_id=?",
                (capacity, resource),
            )
        template = db.execute(
            "INSERT INTO scoring_templates(name,version,weights_json,rules_json,status,created_at) "
            "VALUES ('analytics',1,'{}','{}','active',?)",
            (stamp,),
        ).lastrowid
        model = db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version) "
            "VALUES ('analytics','onnx','analytics',2,'test')"
        ).lastrowid
        dq = {
            "demands": {str(demands[0]): {"weak_only": False}, str(demands[1]): {"weak_only": True}}
        }
        run = db.execute(
            "INSERT INTO match_runs(scope,template_id,embedding_model_id,llm_model,"
            "engine_version,synonyms_version,config_hash,status,started_at,finished_at,"
            "demands_assessed,pairs_scored,roles_without_candidate,dq_summary_json) "
            "VALUES ('full',?,?,'test-model','test','test','test','completed',?,?,2,3,1,?)",
            (template, model, stamp, stamp, json.dumps(dq)),
        ).lastrowid
        results = []
        for demand, resource, score, band in (
            (demands[0], resources[0], 91, "Strong"),
            (demands[1], resources[1], 60, "Weak"),
            (demands[0], resources[2], 63.1, "Avg"),
        ):
            version = db.execute(
                "SELECT current_version_id FROM demands WHERE demand_id=?", (demand,)
            ).fetchone()[0]
            req = db.execute(
                "SELECT jd_req_id FROM jd_requirements WHERE demand_version_id=?", (version,)
            ).fetchone()[0]
            result = db.execute(
                "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,"
                "resource_id,score,band,rank,shortlisted,grade_relation,location_assessment,"
                "matching_evidence,gaps_checks,eligibility_evidence) "
                "VALUES (?,?,?,?,?,?,?,1,1,'exact','Aligned','SQL matched',"
                "'Review availability','Eligible')",
                (run, demand, version, req, resource, score, band),
            ).lastrowid
            results.append(result)
            for component, inputs in (
                ("skills", {}),
                ("grade", {"distance": "0"}),
                ("bench_end", {"parse_status": "blank"}),
            ):
                db.execute(
                    "INSERT INTO score_breakdowns VALUES (?,?,1,1,?)",
                    (result, component, json.dumps(inputs)),
                )
        email_template = db.execute(
            "INSERT INTO email_templates(name,version,subject,body_html,created_at) "
            "VALUES ('analytics',1,'subject','body',?)",
            (stamp,),
        ).lastrowid
        for i, delta in enumerate((86400, 86401, -1, None)):
            sent = (NOW + timedelta(seconds=delta)).isoformat() if delta is not None else None
            db.execute(
                "INSERT INTO email_messages(demand_id,resource_id,send_seq,email_template_id,"
                "to_address,cc_json,subject,body_html,status,idempotency_key,created_by,"
                "approved_at,sent_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'[]','subject','body',?,?,1,?,?,?,?)",
                (
                    demands[0],
                    resources[0],
                    i + 1,
                    email_template,
                    "person0@example.com",
                    "Sent" if sent else "Approved",
                    f"analytics-{i}",
                    stamp,
                    sent,
                    stamp,
                    stamp,
                ),
            )
    return api, {"run": run, "demands": demands, "resources": resources, "results": results}


def get(api: Api, path: str) -> Any:
    response = api[0].get(PREFIX + path)
    assert response.status_code == 200, response.text
    return response.json()


def test_empty_errors_auth_and_openapi(api: Api) -> None:
    client, _ = api
    for panel in PANELS:
        assert client.get(f"{PREFIX}/analytics/{panel}").status_code == 200
        assert client.get(f"{PREFIX}/analytics/{panel}?runId=987654").status_code == 404
    assert get(api, "/analytics/summary")["run"] is None
    assert get(api, "/analytics/match")["bandDistribution"] == []
    assert client.get(f"{PREFIX}/match-runs/999/export").status_code == 404
    bad = client.get(f"{PREFIX}/match-runs/999/export?format=pdf")
    assert bad.status_code == 422 and bad.headers["content-type"].startswith(
        "application/problem+json"
    )
    paths = client.app.openapi()["paths"]
    for path in [*(f"/analytics/{p}" for p in PANELS), "/match-runs/{run_id}/export"]:
        assert PREFIX + path in paths
    client.cookies.clear()
    for path in [*(f"/analytics/{p}" for p in PANELS), "/match-runs/1/export"]:
        assert client.get(PREFIX + path).status_code == 401


def test_shapes_counts_capacity_and_clock(populated: tuple[Api, dict[str, Any]]) -> None:
    api, ids = populated
    s = get(api, "/analytics/summary")
    assert s["requisitionsAssessed"] == 2 and s["roleToPersonCombinations"] == 3
    assert s["openRequisitions"] == 3 and s["matchCoveragePct"] == 33.3
    assert s["rolesWithNoSuitableCandidate"] == 1 and s["weakOnlyRequisitions"] == 1
    assert s["eligibleBench"] == 3 and s["excludedBench"] == 1
    assert s["dataQualityIssues"] == {"total": 2, "dl": 1, "ct": 1}
    assert s["emailsWithin24hPct"] == 25
    assert s["run"]["runId"] == ids["run"]
    d = get(api, "/analytics/demand")
    assert d["topSkills"] == [{"label": "Python", "count": 3}, {"label": "SQL", "count": 3}]
    assert d["startPipeline"][0] == {"label": "Overdue", "count": 1}
    assert len(d["rows"]) == 3 and all(row["jdText"] for row in d["rows"])
    assert {row["jdStatus"] for row in d["rows"]} == {"Scored", "Confirmed — scoring"}
    assert [row["label"] for row in d["gradeToJdCount"]] == [
        "A3",
        "A4",
        "B1",
        "B2",
        "C1",
        "C2",
        "D1",
        "D2",
        "E1",
    ]
    b = get(api, "/analytics/bench")
    assert all(row["supplyY"] == 1 and row["supplyN"] == 2 for row in b["demandVsSupply"])
    assert len(b["timeline"]) == 3 and sum(row["count"] for row in b["aging"]) == 3
    assert b["statusDistribution"][-1] == {"status": "Selected", "count": 1, "excluded": True}
    assert b["rows"][0]["emailAddressMasked"] != "person0@example.com"
    m = get(api, "/analytics/match")
    assert m["coveragePct"] == 33.3
    assert [row["count"] for row in m["bandDistribution"]] == [1, 0, 1, 1, 0]
    assert all(check["passed"] and check["detail"] for check in m["validation"])
    with database(api[0].app.state.settings) as db:
        db.execute("UPDATE demands SET lifecycle='closed' WHERE demand_id=?", (ids["demands"][0],))
        db.execute(
            "UPDATE resources SET lifecycle='off_bench' WHERE resource_id=?", (ids["resources"][0],)
        )
    assert get(api, "/analytics/summary")["emailsWithin24hPct"] == 25


def test_overrides_filters_and_explicit_run(populated: tuple[Api, dict[str, Any]]) -> None:
    api, ids = populated
    client, csrf = api
    response = client.post(
        f"{PREFIX}/match-results/{ids['results'][1]}/overrides",
        json={"action": "adjust_band", "newBand": "Potential", "reason": "review"},
        headers=csrf,
    )
    assert response.status_code == 201, response.text
    m = get(api, f"/analytics/match?runId={ids['run']}")
    assert m["coveragePct"] == 66.7
    assert [row["count"] for row in m["bandDistribution"]] == [1, 1, 1, 0, 0]
    response = client.post(
        f"{PREFIX}/match-results/{ids['results'][0]}/overrides",
        json={"action": "exclude", "reason": "not available"},
        headers=csrf,
    )
    assert response.status_code == 201
    assert get(api, "/analytics/match")["bandDistribution"][0]["count"] == 0
    export = client.get(f"{PREFIX}/match-runs/{ids['run']}/export")
    exported = list(csv.reader(io.StringIO(export.text)))
    assert len(exported) == 5
    assert {row[11] for row in exported[3:]} == {"Potential", "Avg"}
    empty = client.get(f"{PREFIX}/match-runs/{ids['run']}/export?band=Strong")
    assert len(list(csv.reader(io.StringIO(empty.text)))) == 3
    for filter in (
        "codeRequisition=missing",
        "accountName=missing",
        "bu=missing",
        "region=missing",
        "grade=E1",
    ):
        assert get(api, f"/analytics/demand?{filter}")["rows"] == []
    for filter in (
        "location=missing",
        "status=missing",
        "candidateType=Internal",
        "incompleteIdentity=no",
        "grade=E1",
    ):
        assert get(api, f"/analytics/bench?{filter}")["rows"] == []
    for filter in (
        "band=Strong",
        "scoreMin=99",
        "scoreMax=1",
        "mandatorySkills=gap",
        "candidateType=Internal",
        "location=missing",
    ):
        assert (
            sum(row["count"] for row in get(api, f"/analytics/match?{filter}")["bandDistribution"])
            == 0
        )
    assert len(get(api, "/analytics/demand?location=missing&scoreMin=99")["rows"]) == 3
    assert len(get(api, "/analytics/bench?bu=missing&band=Strong")["rows"]) == 4
    assert client.get(f"{PREFIX}/analytics/match?scoreMin=90&scoreMax=10").status_code == 422


@pytest.mark.parametrize(
    "check,statement",
    [
        (
            "bench-end",
            'UPDATE score_breakdowns SET inputs_json=\'{"parse_status":"valid"}\' '
            "WHERE component='bench_end'",
        ),
        (
            "grade",
            'UPDATE score_breakdowns SET inputs_json=\'{"distance":"2"}\' '
            "WHERE component='grade'",
        ),
        ("email-copy", "UPDATE resources SET email_address=NULL"),
        ("evidence", "UPDATE match_results SET gaps_checks=''"),
        ("score", "UPDATE match_results SET score=101"),
        ("start-date", "UPDATE demands SET position_start_date='not a date'"),
    ],
)
def test_validation_failures(
    populated: tuple[Api, dict[str, Any]], check: str, statement: str
) -> None:
    api, _ = populated
    with database(api[0].app.state.settings) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(statement)
    checks = {row["id"]: row for row in get(api, "/analytics/match")["validation"]}
    assert checks[check]["passed"] is False
    assert checks["capacity"]["passed"] and "Structural" in checks["capacity"]["detail"]


def test_unparseable_pending_review(populated: tuple[Api, dict[str, Any]]) -> None:
    api, ids = populated
    with database(api[0].app.state.settings) as db:
        db.execute(
            "UPDATE score_breakdowns SET inputs_json=? WHERE component='bench_end' AND result_id=?",
            (json.dumps({"parse_status": "unparseable"}), ids["results"][0]),
        )
    check = get(api, "/analytics/match")["validation"][0]
    assert check["passed"] and check["detail"].startswith("1 pending-review")


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_exports_layout_emails_injection_and_audit(
    populated: tuple[Api, dict[str, Any]], format: str
) -> None:
    api, ids = populated
    client, _ = api
    dangerous = ["=1+1", " -2", "@x", "\tcmd", "\rcmd"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE demands SET account_name=?,demand_request_name=?,bu=?",
            (dangerous[0], dangerous[1], "Data"),
        )
        db.execute("UPDATE resources SET short_name=?,location=?", (dangerous[2], dangerous[3]))
        db.execute("UPDATE match_results SET gaps_checks=?", (dangerous[4],))
    response = client.get(f"{PREFIX}/match-runs/{ids['run']}/export?format={format}")
    assert response.status_code == 200, response.text
    assert f"PFC-results-run{ids['run']}.{format}" in response.headers["content-disposition"]
    if format == "csv":
        rows = list(csv.reader(io.StringIO(response.text, newline="")))
        assert rows[3][10] == "91.0%"
        assert rows[4][10] == "63.1%"
    else:
        sheet = load_workbook(io.BytesIO(response.content)).active
        assert sheet is not None
        rows = [[cell.value for cell in row] for row in sheet]
        assert sheet.cell(4, 11).value == 0.91 and sheet.cell(4, 11).number_format == "0.0%"
        for column in (3, 4, 7, 10, 15):
            assert sheet.cell(4, column).data_type == "s"
    assert (
        rows[0][0]
        == f"Run {ids['run']} · Finished {NOW.isoformat()} · Model test-model · Template v1"
    )
    assert rows[1][0] == DISCLAIMER and rows[2][:16] == list(HEADERS)
    assert [rows[3][i] for i in (2, 3, 6, 9, 14)] == ["'" + value for value in dangerous]
    assert rows[3][7] != "person0@example.com"
    response = client.get(
        f"{PREFIX}/match-runs/{ids['run']}/export?format={format}&includeEmails=true&band=Avg"
    )
    assert response.status_code == 200
    if format == "csv":
        rows = list(csv.reader(io.StringIO(response.text)))
    else:
        sheet = load_workbook(io.BytesIO(response.content)).active
        assert sheet is not None
        rows = [[cell.value for cell in row] for row in sheet]
    assert len(rows) == 4 and rows[3][7] == "person2@example.com"
    with database(client.app.state.settings) as db:
        audits = db.execute(
            "SELECT action,details_json FROM audit_log "
            "WHERE action IN ('export_results','unmask') ORDER BY audit_id"
        ).fetchall()
        assert [row["action"] for row in audits] == ["export_results", "unmask", "export_results"]
        details = json.loads(audits[-1]["details_json"])
        assert details["run_id"] == ids["run"] and details["rows"] == 1
        assert details["includeEmails"] and details["filters"]["band"] == "Avg"
        assert verify_chain(db)
