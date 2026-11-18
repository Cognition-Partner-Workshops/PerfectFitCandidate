import io
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader
from test_analytics_export import populated as populated
from test_analytics_extended import extended as extended
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import NOW, seed_resource, wait_done

from pfc.api.schemas.reports import PdfRequest
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.db.users import create_user
from pfc.services.pii import mask_emails_in
from pfc.services.reports import data as report_data
from pfc.services.reports import jobs as report_jobs
from pfc.services.reports.builder import DISCLAIMER, METHOD

URL = "/api/v1/reports"
FILTERS = {"bu": ["Data"], "grade": ["C1"], "scoreMin": 61, "mandatorySkills": ["met"]}
RESULT_FILTERS = {
    "bu": ["Data"],
    "demandGrade": ["C1"],
    "scoreMin": 61,
    "mandatorySkills": ["met"],
    "sort": "score",
    "descending": True,
}


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def formatted(value: Any) -> str:
    if value is None or value == "":
        return "Not recorded"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return "; ".join(formatted(v) for v in value) or "None"
    return (
        str(int(value))
        if isinstance(value, float) and value.is_integer()
        else mask_emails_in(str(value))
    )


def generate(api: Api, body: dict[str, Any]) -> tuple[int, bytes, str]:
    client, headers = api
    response = client.post(URL + "/pdf", json=body, headers=headers)
    assert response.status_code == 202, response.text
    job_id = response.json()["jobId"]
    job = wait_done(client.app.state.job_runner, job_id)
    assert job.status == "completed", job.message
    assert job.progress == 1 and job.kind == "pdf_report"
    response = client.get(f"{URL}/{job_id}/file")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    reader = PdfReader(io.BytesIO(response.content))
    for number, page in enumerate(reader.pages, 1):
        assert DISCLAIMER in page.extract_text()
        assert f"Page {number}/{len(reader.pages)}" in page.extract_text()
    return job_id, response.content, normalize(" ".join(p.extract_text() for p in reader.pages))


def assert_row(text: str, *values: Any) -> None:
    expected = normalize(" ".join(formatted(v) for v in values))
    assert expected in text, expected


def assert_dashboard(text: str, panels: dict[str, Any]) -> None:
    s, d, b, m, e = (panels[k] for k in ("summary", "demand", "bench", "match", "extended"))
    for label, key in [
        ("Requisitions assessed", "requisitionsAssessed"),
        ("Open requisitions", "openRequisitions"),
        ("Role-to-person combinations", "roleToPersonCombinations"),
        ("Roles with no suitable candidate", "rolesWithNoSuitableCandidate"),
        ("Weak-only requisitions", "weakOnlyRequisitions"),
        ("Eligible bench", "eligibleBench"),
        ("Excluded bench", "excludedBench"),
    ]:
        assert_row(text, label, s[key])
    for label, key in [
        ("Data-quality limitations", "total"),
        ("DL limitations", "dl"),
        ("CT limitations", "ct"),
    ]:
        assert_row(text, label, s["dataQualityIssues"][key])
    assert_row(text, "Match coverage", f"{formatted(s['matchCoveragePct'])} %")
    assert_row(text, "Emails sent ≤ 24 h", f"{formatted(s['emailsWithin24hPct'])} %")
    ratio = (
        s["rolesWithNoSuitableCandidate"] / s["requisitionsAssessed"] * 100
        if s["requisitionsAssessed"]
        else 0
    )
    assert_row(text, "Roles without suitable candidate (% of assessed)", f"{ratio:.1f} %")
    assert f"{formatted(m['coveragePct'])} %" in text
    datasets = [
        (d, "openByBuRegion", ["bu", "region", "count"]),
        *[
            (d, key, ["label", "count"])
            for key in ("topSkills", "topAccounts", "startPipeline", "gradeToJdCount")
        ],
        (b, "demandVsSupply", ["skill", "demandCount", "supplyY", "supplyN"]),
        (b, "gradeHistogram", ["grade", "bench", "demand"]),
        (b, "statusDistribution", ["status", "count", "excluded"]),
        *[(b, key, ["label", "count"]) for key in ("topSkills", "aging")],
        *[(m, key, ["label", "count"]) for key in ("bandDistribution", "emailStatusCounts")],
        *[
            (e, key, ["label", "count"])
            for key in ("shortlistFunnel", "exclusionReasons", "candidateTypes")
        ],
        (e, "gradeMatrix", ["row", "column", "count", "demandCount", "benchCount", "relation"]),
        *[
            (e, key, ["row", "column", "count"])
            for key in ("readinessStatus", "locationMatrix", "skillCooccurrence")
        ],
        (e["scoreHistogram"], "bins", ["lower", "upper", "allPairs", "top5"]),
        (e, "emailTrend", ["week", "sentCount", "medianHours", "within24hPct"]),
        (
            e,
            "accuracy",
            ["runId", "confirmedDemands", "confirmedPairs", "hits", "hitRateAt5", "precisionAt5"],
        ),
        (e, "contention", ["name", "emailMasked", "demandCount", "requisitions"]),
        (e, "overrideRates", ["runId", "pairCount", "overriddenPairs", "ratePct"]),
        (e["demandAging"], "buckets", ["label", "count"]),
        (m, "validation", ["label", "passed", "detail"]),
    ]
    for panel, key, fields in datasets:
        for row in panel[key]:
            assert_row(text, *(row[f] for f in fields))
    for row in b["timeline"]:
        start = date.fromisoformat(row["start"][:10]).strftime("%d %b %Y")
        assert_row(text, row["name"], start, row["days"])
    for row in e["unmatchedDemands"]:
        start = date.fromisoformat(row["positionStartDate"][:10]).strftime("%d %b %Y")
        assert_row(text, row["codeRequisition"], start, row["reasonCodes"])
    for row in e["upskilling"]:
        assert_row(
            text,
            row["name"],
            row["emailMasked"],
            row["matchedSkills"],
            row["missingSkills"],
            f"{int(row['bestScore'] + 0.5)} %" if row["bestScore"] is not None else None,
        )
    for row in e["overrideRates"]:
        for action in row["byType"]:
            assert_row(text, row["runId"], action["label"], action["count"])
    assert_row(text, "Unknown start", e["demandAging"]["unknownStart"])
    assert_row(text, "Past start without match", e["demandAging"]["pastStartWithoutMatch"])
    assert normalize(METHOD) in text


@pytest.mark.parametrize("scope", ["filters", "all"])
def test_p2_ex04_equivalence_and_audit(extended: tuple[Api, dict[str, Any]], scope: str) -> None:
    api, ids = extended
    client, _ = api
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE match_results SET matching_evidence='Contact person0@example.com', "
            "gaps_checks='Check person0@example.com', "
            "eligibility_evidence='Verify person0@example.com'"
        )
    panels = {}
    for panel in ("summary", "demand", "bench", "match", "extended"):
        response = client.get(
            f"/api/v1/analytics/{panel}", params=FILTERS if scope == "filters" else {}
        )
        assert response.status_code == 200, response.text
        panels[panel] = response.json()
    job_id, dashboard_pdf, text = generate(
        api, {"report": "dashboard", "scope": scope, "filters": FILTERS}
    )
    assert_dashboard(text, panels)
    assert "Generated by: Reviewer" in text and "Template v1" in text
    assert f"Run #{ids['run']}" in text and "test-model" in text and "data.xlsx" in text
    for kind, digest in [("DL", "0"), ("DL", "1"), ("CT", "2")]:
        assert_row(text, kind, "data.xlsx", digest)
    response = client.get(
        "/api/v1/match-results/current", params=RESULT_FILTERS if scope == "filters" else {}
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    _, results_pdf, results_text = generate(
        api, {"report": "match_results", "scope": scope, "resultFilters": RESULT_FILTERS}
    )
    positions = []
    grouped = [
        row
        for req in dict.fromkeys(r["codeRequisition"] for r in rows)
        for row in rows
        if row["codeRequisition"] == req
    ]
    for row in grouped:
        cells = [
            row["codeRequisition"],
            row["rank"],
            row["personName"],
            row["personEmailMasked"],
            row["demandGrade"],
            row["personGrade"],
            row["personLocation"],
            f"{int(row['score'] + 0.5)} %",
            row["effectiveBand"],
            row["locationAssessment"],
        ]
        expected = normalize(" ".join(formatted(v) for v in cells))
        positions.append(results_text.index(expected))
        for label, key in [
            ("Account", "accountName"),
            ("Role", "demandRequestName"),
            ("Region", "region"),
        ]:
            assert_row(results_text, label, row[key])
        assert_row(
            results_text,
            "Start date",
            date.fromisoformat(row["positionStartDate"]).strftime("%d %b %Y"),
        )
        for key in ("matchingEvidence", "gapsChecks", "eligibilityEvidence"):
            for part in row[key]:
                assert normalize(part) in results_text
    assert positions == sorted(positions)
    assert "person0@example.com" not in text + results_text
    assert "pe*****@example.com" in results_text
    with database(client.app.state.settings) as db:
        audits = [
            dict(row) for row in db.execute("SELECT * FROM audit_log WHERE action LIKE 'PDF_%'")
        ]
        assert len(audits) == 4
        assert verify_chain(db)
    generated = json.loads(next(a["details_json"] for a in audits if a["action"] == "PDF_GENERATE"))
    assert generated["job_id"] == job_id and generated["scope"] == scope
    assert generated["row_count"] == len(panels["demand"]["rows"])
    assert "filters" in generated and generated["sections"]
    listing = client.get("/api/v1/jobs").json()
    card = next(item for item in listing if item["jobId"] == job_id)
    assert card["label"] == "PDF report" and card["link"] == f"{URL}/{job_id}/file"
    assert not list((client.app.state.settings.data_dir / "reports").glob("*.part"))
    if (output := os.environ.get("PFC_PDF_SAMPLE_DIR")) and scope == "all":
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "dashboard.pdf").write_bytes(dashboard_pdf)
        with database(client.app.state.settings) as db:
            for number in range(40):
                resource = seed_resource(
                    db,
                    f"Sample candidate {number + 1:02d}",
                    email=f"sample{number + 1:02d}@example.com",
                )
                db.execute(
                    "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,"
                    "resource_id,score,band,rank,shortlisted,grade_relation,location_assessment,"
                    "matching_evidence,gaps_checks,eligibility_evidence) "
                    "SELECT run_id,demand_id,demand_version_id,jd_req_id,?,score,band,?,"
                    "shortlisted,grade_relation,location_assessment,matching_evidence,"
                    "gaps_checks,eligibility_evidence "
                    "FROM match_results WHERE result_id=?",
                    (resource, number + 7, ids["results"][0]),
                )
        _, results_pdf, _ = generate(api, {"report": "match_results", "scope": "all"})
        (folder / "match_results.pdf").write_bytes(results_pdf)


@pytest.mark.parametrize(
    "body",
    [
        {"report": "unknown"},
        {"report": "dashboard", "sections": ["unknown"]},
        {"report": "dashboard", "sections": ["summary", "summary"]},
        {"report": "dashboard", "sections": ["match-results"]},
        {"report": "match_results", "sections": ["cover"]},
        {"report": "dashboard", "orientation": "diagonal"},
        {"report": "dashboard", "filters": {"scoreMin": 90, "scoreMax": 2}},
        {"report": "match_results", "resultFilters": {"includeEmails": True}},
        {"report": "match_results", "resultFilters": {"limit": 5001}},
    ],
)
def test_validation(api: Api, body: dict[str, Any]) -> None:
    client, headers = api
    assert client.post(URL + "/pdf", json=body, headers=headers).status_code == 422
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_missing_run_scope_all_and_empty(api: Api) -> None:
    client, headers = api
    body = {"report": "dashboard", "filters": {"runId": 999}}
    assert client.post(URL + "/pdf", json=body, headers=headers).status_code == 404
    _, _, text = generate(api, {**body, "scope": "all", "sections": ["summary"]})
    assert_row(text, "Requisitions assessed", 0)
    _, _, text = generate(api, {"report": "match_results"})
    assert "No results for this selection." in text


def test_download_authorization_and_auth(api: Api) -> None:
    client, headers = api
    job_id, _, _ = generate(api, {"report": "match_results"})
    with database(client.app.state.settings) as db:
        other = create_user(
            db,
            email="other@example.com",
            display_name="Other",
            role_id=1,
            password_hash="unused",
            now=NOW.isoformat(),
        )
        db.execute("UPDATE jobs SET created_by=? WHERE job_id=?", (other.user_id, job_id))
    role = client.get("/api/v1/auth/me").json()["user"]["role"]
    assert client.get(f"{URL}/{job_id}/file").status_code == (200 if role == "Admin" else 404)
    assert client.get(f"{URL}/{job_id + 100}/file").status_code == 404
    with database(client.app.state.settings) as db:
        db.execute("UPDATE jobs SET status='failed' WHERE job_id=?", (job_id,))
    assert client.get(f"{URL}/{job_id}/file").status_code == 404
    assert client.post(URL + "/pdf", json={"report": "dashboard"}).status_code == 403
    client.cookies.clear()
    assert client.get(f"{URL}/{job_id}/file").status_code == 401
    assert (
        client.post(URL + "/pdf", json={"report": "dashboard"}, headers=headers).status_code == 401
    )


def test_truncation_and_result_paging(
    populated: tuple[Api, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = populated
    client, _ = api
    assert report_data.MAX_RESULTS == 5000
    monkeypatch.setattr(report_data, "MAX_RESULTS", 2)
    _, _, text = generate(
        api, {"report": "match_results", "resultFilters": {"sort": "score", "descending": True}}
    )
    assert "truncated: 2 of 3" in text and "91 %" in text and "63 %" in text and "60 %" not in text
    _, _, text = generate(
        api,
        {
            "report": "match_results",
            "resultFilters": {"sort": "score", "descending": True, "limit": 1, "offset": 1},
        },
    )
    assert "truncated: 1 of 3" in text and "63 %" in text and "91 %" not in text
    assert client.get("/api/v1/match-results/current").headers["X-Total-Count"] == "3"


def test_failure_is_not_downloadable(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_: Any) -> bytes:
        raise RuntimeError("failure@example.com")

    client, headers = api
    monkeypatch.setattr(report_jobs, "build_pdf", broken)
    response = client.post(URL + "/pdf", json={"report": "match_results"}, headers=headers)
    job_id = response.json()["jobId"]
    assert wait_done(client.app.state.job_runner, job_id).status == "failed"
    assert client.get(f"{URL}/{job_id}/file").status_code == 404
    assert not (client.app.state.settings.data_dir / "reports" / f"{job_id}.pdf").exists()


def test_generation_audit_masks_filter_emails() -> None:
    request = PdfRequest(report="match_results", result_filters={"account": ["person@example.com"]})
    details = json.dumps(report_jobs.audit_details(request))
    assert "person@example.com" not in details and "pe****@example.com" in details
