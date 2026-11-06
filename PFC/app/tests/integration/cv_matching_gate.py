"""Offline CT/CV comparison using production jobs and the matching-test embedder."""

import argparse
import json
from pathlib import Path

from test_matching_service import (
    execute,
    seed_candidate,
    seed_cv,
    seed_demand,
    seed_resource,
)
from test_matching_service import settings as settings_fixture

from pfc.db.connection import database
from pfc.domain.config_loader import load_domain_config
from pfc.jobs.runner import JobRunner
from pfc.services.ingestion.cv_facts import FACTS_SCHEMA_VERSION, rule_facts
from pfc.services.matching import MatchingPorts
from tests.perf.test_nfr01 import FakeEmbedder

FIXTURES = Path(__file__).parents[1] / "fixtures"


def capture(data_dir: Path) -> list[dict]:
    settings = settings_fixture.__wrapped__(data_dir)
    fixture = json.loads((FIXTURES / "cv_matching_gate.json").read_text())
    demands = json.loads((FIXTURES / "llm_validation.json").read_text())["jds"]
    config = load_domain_config()
    with database(settings) as db:
        for demand in demands:
            if demand["id"] in fixture["demands"]:
                seed_demand(
                    db,
                    demand["id"],
                    jd=demand["text"],
                    mandatory=demand["expected"]["mandatory_skills"],
                    preferred=demand["expected"]["preferred_skills"],
                )
        for person in fixture["candidates"]:
            candidate = seed_candidate(db)
            db.execute(
                "UPDATE candidates SET internal_ref=? WHERE candidate_id=?",
                (f"PFC-GATE-{person['name']}", candidate),
            )
            resource = seed_resource(db, person["name"], skills=person["ct"], candidate=candidate)
            version = seed_cv(db, resource, candidate, skills=person["cv"])
            db.execute(
                "UPDATE resumes SET file_sha256=? WHERE candidate_id=?",
                (f"gate-{person['name']}", candidate),
            )
            db.execute(
                "UPDATE resource_skills SET canonical_skill_id="
                "(SELECT skill_id FROM skills WHERE name=resource_skills.skill_raw COLLATE NOCASE) "
                "WHERE cv_version_id=?",
                (version,),
            )
            text = "Skills\n" + "; ".join(person["cv"])
            facts = rule_facts(text)
            for skill in facts.skills:
                skill.canonical = config.synonyms.resolve(skill.value)
            db.execute(
                "UPDATE cv_versions SET text_norm=?,facts_json=? WHERE cv_version_id=?",
                (
                    text,
                    json.dumps({"facts_schema": FACTS_SCHEMA_VERSION, "facts": facts.model_dump()}),
                    version,
                ),
            )
    runner = JobRunner(settings)
    try:
        run_id = execute(settings, runner, MatchingPorts(FakeEmbedder()))
        with database(settings) as db:
            output = []
            for row in db.execute(
                "SELECT d.code_requisition,r.short_name,m.score,m.band,m.rank,m.mandatory_missing,"
                "b.inputs_json FROM match_results m JOIN resources r USING(resource_id) "
                "JOIN demands d USING(demand_id) JOIN score_breakdowns b USING(result_id) "
                "WHERE m.run_id=? AND b.component='skills' "
                "ORDER BY d.code_requisition,r.short_name",
                (run_id,),
            ):
                item = dict(row)
                item["inputs"] = json.loads(item.pop("inputs_json"))
                output.append(item)
            assert len(output) == 30
            return output
    finally:
        runner.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(json.dumps(capture(args.data_dir), indent=2) + "\n", encoding="utf-8")
