"""Deterministic synthetic data built on the application's real migrations."""

import json
import random
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook  # type: ignore[import-untyped]

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services.ingestion import service as ingestion
from pfc.services.ingestion.workbooks import CT
from pfc.services.occ.seed import seed_reference_data

TODAY = "2026-11-06"
SKILLS = ("SQL", "Python", "Java", "Databricks", "AWS", "Azure")
GRADES = ("B1", "B2", "C1", "C2", "D1")
REGIONS = ("London", "Manchester", "Edinburgh", "Bristol")


def ingest_resources(settings: Settings) -> None:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(list(CT))
    for i in range(1, 81):
        values = {
            "ShortName": f"SYNTHETIC_PERSON_{i}",
            "Email Address": f"fake{i}@example.invalid",
            "Grade": GRADES[i % 5],
            "Location": REGIONS[i % 4],
            "Bench Status": "Available",
            "Skills": SKILLS[i % 6],
            "Available Capacity": ("Y", "N", None)[i % 3],
            "Bench Start Date": "2026-10-01",
            "Bench End Date": "2026-12-31",
            "Group": "RG1",
        }
        sheet.append([values.get(header) for header in CT])
    stream = BytesIO()
    book.save(stream)
    book.close()
    run = ingestion.upload(settings, "CT", "synthetic.xlsx", stream.getvalue(), 1, TODAY)
    ingestion.commit(settings, run.ingestion_run_id, 1, TODAY)


def build_fixture(root: Path) -> Settings:
    settings = Settings(data_dir=root)
    migrate(settings)
    rng = random.Random(1729)
    with database(settings) as db:
        if db.execute("SELECT count(*) FROM demands").fetchone()[0]:
            raise ValueError("Eval fixture requires an empty database")
        seed_reference_data(db)
        for skill in SKILLS:
            db.execute(
                "INSERT INTO skills(name,created_by) VALUES (?,'system') "
                "ON CONFLICT(name) DO NOTHING",
                (skill,),
            )
        db.execute(
            "INSERT INTO users(user_id,email,display_name,role_id,password_hash,created_at,"
            "updated_at) VALUES (1,'fixture@example.invalid','SYNTHETIC_USER',1,'unused',?,?)",
            (TODAY, TODAY),
        )
    ingest_resources(settings)
    with database(settings) as db:
        skills = {r["name"]: r["skill_id"] for r in db.execute("SELECT * FROM skills")}
        for i in range(1, 51):
            db.execute(
                "INSERT INTO demands(demand_id,code_requisition,id_format_ok,account_name,bu,"
                "region,code_local_grade,position_start_date,lifecycle,row_hash,"
                "created_at,updated_at) "
                "VALUES (?,?,1,?,?,?,?,?,?,?, ?,?)",
                (
                    i,
                    f"T{10000 + i}",
                    f"Account {i % 5 + 1}",
                    ("INS", "FS", "MFG")[i % 3],
                    REGIONS[i % 4],
                    GRADES[i % 5],
                    f"2026-{10 + i % 3:02}-15",
                    "archived" if i > 47 else "open",
                    f"d{i}",
                    TODAY,
                    TODAY,
                ),
            )
            db.execute(
                "INSERT INTO demand_versions(demand_version_id,demand_id,version_no,jd_text_raw,"
                "jd_text_norm,jd_sha256,created_at) VALUES (?,?,1,?,?,?,?)",
                (i, i, "Synthetic JD", "Synthetic JD", f"jd{i}", TODAY),
            )
            db.execute("UPDATE demands SET current_version_id=? WHERE demand_id=?", (i, i))
            db.execute(
                "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,"
                "created_at) "
                "VALUES (?,?,1,'confirmed',?)",
                (i, i, TODAY),
            )
            for offset, kind in enumerate(("mandatory_skill", "preferred_skill")):
                skill = SKILLS[(i + offset) % 6]
                db.execute(
                    "INSERT INTO jd_requirement_items(jd_req_id,kind,text,canonical_skill_id,"
                    "position) "
                    "VALUES (?,?,?,?,?)",
                    (i, kind, skill, skills[skill], offset),
                )
        for i in range(1, 81):
            candidate = db.execute(
                "SELECT candidate_id FROM resources WHERE resource_id=?", (i,)
            ).fetchone()[0]
            db.execute(
                "UPDATE resources SET eligibility=?,lifecycle=? WHERE resource_id=?",
                (
                    "excluded" if i % 7 == 0 else "eligible",
                    "archived" if i > 76 else "off_bench" if i % 9 == 0 else "active",
                    i,
                ),
            )
            resume = db.execute(
                "INSERT INTO resumes(candidate_id,source_path,file_name,file_sha256,mime_type,"
                "discovered_at) VALUES (?,'synthetic.pdf','synthetic.pdf',?,'application/pdf',?)",
                (candidate, f"synthetic-{i}", TODAY),
            ).lastrowid
            cv = db.execute(
                "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
                "extracted_by,is_latest,extracted_at) VALUES (?,?,1,?,'','synthetic',1,?)",
                (resume, candidate, f"synthetic-cv-{i}", TODAY),
            ).lastrowid
            for offset in (1, 2):
                skill = SKILLS[(i + offset) % 6]
                db.execute(
                    "INSERT INTO resource_skills(resource_id,source,skill_raw,skill_norm,"
                    "canonical_skill_id,years,cv_version_id) VALUES (?,?,?,?,?,?,?)",
                    (
                        i,
                        "CV",
                        skill,
                        skill.lower(),
                        skills[skill],
                        float(i % 10 + 1),
                        cv,
                    ),
                )
        db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version) "
            "VALUES ('synthetic','onnx','synthetic',2,'eval')"
        )
        template = db.execute(
            "SELECT template_id FROM scoring_templates WHERE status='active'"
        ).fetchone()[0]
        model = db.execute(
            "SELECT embedding_model_id FROM embedding_models WHERE name='synthetic'"
        ).fetchone()[0]
        for run in (1, 2):
            db.execute(
                "INSERT INTO match_runs(run_id,scope,template_id,embedding_model_id,engine_version,"
                "synonyms_version,config_hash,status,dq_summary_json) "
                "VALUES (?,'full',?,?, 'eval','eval','eval','completed',?)",
                (run, template, model, json.dumps({"demands": {str(i): {} for i in range(1, 51)}})),
            )
            for demand in range(1, 51):
                resources = rng.sample(range(1, 81), 80)
                for rank, resource in enumerate(resources, 1):
                    score = round(100 - rank * 0.9 + (run - 1) * 0.01, 3)
                    db.execute(
                        "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,"
                        "resource_id,score,band,rank,shortlisted,mandatory_missing,grade_relation,"
                        "location_assessment,matching_evidence,gaps_checks,eligibility_evidence) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,'exact','Aligned','{}','[]','[]')",
                        (
                            run,
                            demand,
                            demand,
                            demand,
                            resource,
                            score,
                            "Strong"
                            if rank <= 10
                            else "Potential"
                            if rank <= 30
                            else "Avg"
                            if rank <= 50
                            else "Weak",
                            rank,
                            int(rank <= 5),
                            int(rank > 30),
                        ),
                    )
    return settings
