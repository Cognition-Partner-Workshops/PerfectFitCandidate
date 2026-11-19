"""Deterministic synthetic data built on the application's real migrations."""

import json
import random
from pathlib import Path

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services.occ.seed import seed_reference_data

TODAY = "2026-11-06"
SKILLS = ("SQL", "Python", "Java", "Databricks", "AWS", "Azure")
GRADES = ("B1", "B2", "C1", "C2", "D1")
REGIONS = ("London", "Manchester", "Edinburgh", "Bristol")


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
            db.execute(
                "INSERT INTO resources(resource_id,short_name,short_name_key,email_address,"
                "email_key,"
                "grade,location,bench_status_code,bench_start_date,bench_end_date,available_capacity,"
                "eligibility,lifecycle,row_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    i,
                    f"SYNTHETIC_PERSON_{i}",
                    f"synthetic_person_{i}",
                    f"fake{i}@example.invalid",
                    f"fake{i}@example.invalid",
                    GRADES[i % 5],
                    REGIONS[i % 4],
                    "AVAILABLE",
                    "2026-10-01",
                    "2026-12-31",
                    str((i % 4 + 1) * 25),
                    "excluded" if i % 7 == 0 else "eligible",
                    "archived" if i > 76 else "off_bench" if i % 9 == 0 else "active",
                    f"r{i}",
                    TODAY,
                    TODAY,
                ),
            )
            for offset in range(3):
                skill = SKILLS[(i + offset) % 6]
                db.execute(
                    "INSERT INTO resource_skills(resource_id,source,skill_raw,skill_norm,"
                    "canonical_skill_id,years) VALUES (?,?,?,?,?,?)",
                    (
                        i,
                        "CT" if offset == 0 else "CV",
                        skill,
                        skill.lower(),
                        skills[skill],
                        float(i % 10 + 1),
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
