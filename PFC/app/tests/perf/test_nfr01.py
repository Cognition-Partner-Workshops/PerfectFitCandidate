"""Offline NFR-01 regression; timings are not target-laptop acceptance evidence."""

import ctypes
import hashlib
import json
import os
import platform
import random
import sqlite3
import statistics
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient

from pfc.config import Settings
from pfc.db.connection import database
from pfc.domain.config_loader import load_domain_config
from pfc.domain.scoring import build_ct_basis
from pfc.main import create_app
from pfc.security.setup import code_path
from pfc.services.matching import MatchingPorts
from pfc.services.matching.ports import ChunkOwner, Task
from pfc.services.matching.runs import _inputs
from pfc.services.matching.semantic import VectorCache

SEED = 19401
SIZE = 500
SAMPLES = 3
MATCH_LIMIT_SECONDS = 180.0
ANALYTICS_LIMIT_SECONDS = 35.0
NOW = datetime(2026, 11, 6, tzinfo=UTC)
SKILLS = (
    "SQL",
    "Python",
    "Java",
    "AWS",
    "Azure",
    "Business Analysis",
    "Agile",
    "Scrum",
    "Power BI",
    "Excel",
)
LOCATIONS = ("Manchester", "London", "Birmingham", "Leeds")
PASSWORD = "Strong!Random4567"


class FakeEmbedder:
    name = "nfr01-deterministic"
    runtime: Literal["onnx"] = "onnx"
    file_sha256 = "a" * 64
    dim = 8
    pipeline_version = "nfr01-v1"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str], *, task: Task = "document") -> list[list[float]]:
        self.calls += 1
        return [
            [(byte + 1) / 256 for byte in hashlib.sha256(text.encode()).digest()[: self.dim]]
            for text in texts
        ]


class FakeVectorStore:
    def __init__(self) -> None:
        self.values: dict[ChunkOwner, list[float]] = {}

    def get_vectors(self, owners: list[ChunkOwner]) -> list[list[float] | None]:
        return [self.values.get(owner) for owner in owners]

    def upsert(self, owners: list[ChunkOwner], vectors: list[list[float]]) -> None:
        self.values.update(zip(owners, vectors, strict=True))


def seed_population(db: sqlite3.Connection) -> None:
    rng = random.Random(SEED)
    stamp = NOW.isoformat()
    grades = load_domain_config().grades.codes
    db.executemany(
        "INSERT INTO skills(name,created_by) VALUES (?,'seed') ON CONFLICT(name) DO NOTHING",
        [(skill,) for skill in SKILLS],
    )
    skill_ids = {row["name"]: row["skill_id"] for row in db.execute("SELECT * FROM skills")}
    for index in range(1, SIZE + 1):
        skills = rng.sample(SKILLS, 3)
        jd = "Required: " + ", ".join(skills[:2]) + ". Preferred: " + skills[2]
        start = (NOW.date() + timedelta(days=rng.randrange(-30, 91))).isoformat()
        location = rng.choice(LOCATIONS)
        db.execute(
            "INSERT INTO demands(demand_id,code_requisition,id_format_ok,position_start_date,"
            "account_name,bu,code_local_grade,demand_request_name,region,lifecycle,row_hash,"
            "created_at,updated_at) VALUES (?,?,1,?,?,?,?,?,?,'open',?,?,?)",
            (
                index,
                f"T{index:05d}",
                start,
                f"Account {index % 8}",
                f"BU {index % 4}",
                grades[(index - 1) % len(grades)],
                "Data delivery",
                location,
                str(index),
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO demand_versions(demand_version_id,demand_id,version_no,jd_text_raw,"
            "jd_text_norm,jd_sha256,created_at) VALUES (?,?,1,?,?,?,?)",
            (index, index, jd, jd, hashlib.sha256(jd.encode()).hexdigest(), stamp),
        )
        db.execute("UPDATE demands SET current_version_id=? WHERE demand_id=?", (index, index))
        db.execute(
            "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,"
            "location_norm,onsite_required,confirmed_by,confirmed_at,created_at) "
            "VALUES (?,?,1,'confirmed',?,0,1,?,?)",
            (index, index, location, stamp, stamp),
        )
        db.executemany(
            "INSERT INTO jd_requirement_items(jd_req_id,kind,text,canonical_skill_id,position) "
            "VALUES (?,?,?,?,?)",
            [
                (
                    index,
                    "mandatory_skill" if n < 2 else "preferred_skill",
                    skill,
                    skill_ids[skill],
                    n,
                )
                for n, skill in enumerate(skills)
            ],
        )
        db.execute(
            "INSERT INTO candidates(candidate_id,internal_ref,identity_status,classification,"
            "created_at,updated_at) VALUES (?,?,'complete','Internal',?,?)",
            (index, f"PFC-C-PERF-{index}", stamp, stamp),
        )
        excluded = index % 5 == 0
        status = (
            "Selected"
            if excluded
            else (
                "Available",
                "Overall upskilling (New bench)",
                "CV with client - Demand Proposed",
            )[index % 3]
        )
        db.execute(
            "INSERT INTO resources(resource_id,candidate_id,short_name,short_name_key,"
            "email_address,grade,location,bench_status,skills_raw,available_capacity,"
            "bench_start_date,bench_end_date_parse_status,group_name,eligibility,"
            "exclusion_reason,lifecycle,row_hash,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'blank','RG1',?,?,'active',?,?,?)",
            (
                index,
                index,
                f"Person {index}",
                f"person {index}",
                f"person{index}@example.test",
                grades[(index - 1) % len(grades)],
                rng.choice(LOCATIONS),
                status,
                ", ".join(resource_skills := rng.sample(SKILLS, 4)),
                ("Y", "N", None)[index % 3],
                (NOW.date() - timedelta(days=rng.randrange(1, 181))).isoformat(),
                "excluded" if excluded else "eligible",
                "STATUS_EXCLUDED" if excluded else None,
                str(index),
                stamp,
                stamp,
            ),
        )
        db.executemany(
            "INSERT INTO resource_skills(resource_id,source,skill_raw,skill_norm,"
            "canonical_skill_id) VALUES (?,'CT',?,?,?)",
            [(index, skill, skill.casefold(), skill_ids[skill]) for skill in resource_skills],
        )
    assert db.execute("SELECT COUNT(*) FROM demands WHERE lifecycle='open'").fetchone()[0] == SIZE
    assert (
        db.execute("SELECT COUNT(*) FROM jd_requirements WHERE status='confirmed'").fetchone()[0]
        == SIZE
    )
    assert (
        db.execute("SELECT COUNT(*) FROM resources WHERE eligibility='eligible'").fetchone()[0]
        == 400
    )
    assert (
        db.execute("SELECT COUNT(*) FROM resources WHERE eligibility='excluded'").fetchone()[0]
        == 100
    )
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def prime_vectors(db: sqlite3.Connection, ports: MatchingPorts) -> None:
    config = load_domain_config()
    demands, resources, _, metadata, skipped = _inputs(db, "full", (), config)
    assert len(demands) == len(resources) == SIZE and not skipped
    cache = VectorCache(ports)
    for demand in demands:
        meta = metadata[demand.code_requisition]
        cache.vectors("jd", meta["current_version_id"], [meta["jd_text_norm"]])
        for item in demand.requirements:
            cache.vectors("jd_item", int(item.item_id), [item.text], task="query")
    for resource in resources:
        basis = build_ct_basis(resource, config)
        cache.vectors(
            "ct_skills",
            int(resource.resource_id),
            [f"Skill: {skill.raw}\nEvidence: {skill.evidence}" for skill in basis.skills],
        )


def provenance() -> dict[str, Any]:
    cpu = platform.processor()
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        cpu = next(
            (
                line.split(":", 1)[1].strip()
                for line in cpuinfo.read_text().splitlines()
                if line.startswith("model name")
            ),
            cpu,
        )
    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint32),
                ("load", ctypes.c_uint32),
                *[
                    (name, ctypes.c_uint64)
                    for name in (
                        "total_phys",
                        "avail_phys",
                        "total_page",
                        "avail_page",
                        "total_virtual",
                        "avail_virtual",
                        "avail_extended",
                    )
                ],
            ]

        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        assert ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
        ram = memory.total_phys
    else:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    cwd = Path(__file__).parent
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu_model": cpu,
        "logical_cpus": os.cpu_count(),
        "ram_bytes": ram,
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True
        ).strip(),
        "working_tree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=cwd,
                text=True,
            ).strip()
        ),
        "cpu_quota": Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        if Path("/sys/fs/cgroup/cpu.max").exists()
        else None,
        "memory_limit": Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if Path("/sys/fs/cgroup/memory.max").exists()
        else None,
    }


def submit_and_wait(client: TestClient, body: dict[str, Any], csrf: str) -> tuple[int, float]:
    started = time.perf_counter()
    response = client.post("/api/v1/match-runs", json=body, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 202, response.text
    accepted = response.json()
    while True:
        job = client.app.state.job_runner.get(accepted["jobId"])
        assert job is not None
        if job.status in {"completed", "failed", "cancelled"}:
            assert job.status == "completed", job
            return accepted["runId"], time.perf_counter() - started
        if time.perf_counter() - started > MATCH_LIMIT_SECONDS:
            client.app.state.job_runner.cancel(accepted["jobId"])
            pytest.fail("Matching job exceeded the CI-safe timeout")
        time.sleep(0.1)


@pytest.mark.perf
def test_nfr01_matching_and_analytics(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        frontend_dir=tmp_path / "no-frontend",
        allowed_hosts=["testserver"],
        llm_enabled=False,
        job_workers=1,
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    app = create_app(settings)
    app.state.clock = lambda: NOW
    embedder = FakeEmbedder()
    app.state.matching_ports = ports = MatchingPorts(embedder, vector_store=FakeVectorStore())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/setup/admin",
            json={
                "setup_code": code_path(settings).read_text(),
                "email": "perf@example.test",
                "display_name": "Synthetic Perf",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 201, response.text
        response = client.post(
            "/api/v1/auth/login",
            json={
                "role": "Admin",
                "email": "perf@example.test",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 200, response.text
        csrf = response.json()["csrf_token"]
        with database(settings) as db:
            seed_population(db)
            prime_vectors(db, ports)
        calls = embedder.calls
        submit_and_wait(client, {"scope": "demands", "demandIds": [1]}, csrf)
        run_id, match_seconds = submit_and_wait(client, {"scope": "full"}, csrf)
        assert embedder.calls == calls, "The timed run must use only cached vectors"
        with database(settings) as db:
            run = db.execute("SELECT * FROM match_runs WHERE run_id=?", (run_id,)).fetchone()
            assert run["status"] == "completed" and run["demands_assessed"] == SIZE
            pairs = run["pairs_scored"]
            assert pairs > 50000, "Do not silently benchmark an empty or mostly excluded run"
        analytics: dict[str, Any] = {}
        for panel in ("summary", "demand", "bench", "match"):
            url = f"/api/v1/analytics/{panel}?runId={run_id}"
            response = client.get(url)
            assert response.status_code == 200, response.text
            samples = []
            for _ in range(SAMPLES):
                started = time.perf_counter()
                response = client.get(url)
                samples.append(time.perf_counter() - started)
                assert response.status_code == 200, response.text
            payload = response.json()
            if panel == "summary":
                assert payload["requisitionsAssessed"] == payload["openRequisitions"] == SIZE
                assert payload["eligibleBench"] == 400 and payload["excludedBench"] == 100
            elif panel in {"demand", "bench"}:
                assert len(payload["rows"]) == SIZE
            else:
                assert sum(item["count"] for item in payload["bandDistribution"]) == pairs
            analytics[panel] = {
                "samples_seconds": samples,
                "median_seconds": statistics.median(samples),
            }
        report = {
            "label": "Linux CI/VM, not target laptop",
            "seed": SEED,
            "demands": SIZE,
            "resources": SIZE,
            "pairs_scored": pairs,
            "match_seconds": match_seconds,
            "analytics": analytics,
            "provenance": provenance(),
            "thresholds_seconds": {
                "match": MATCH_LIMIT_SECONDS,
                "analytics_median": ANALYTICS_LIMIT_SECONDS,
            },
        }
        print("\nNFR01_RESULT=" + json.dumps(report, sort_keys=True))
        assert match_seconds <= MATCH_LIMIT_SECONDS
        for panel, timing in analytics.items():
            assert timing["median_seconds"] <= ANALYTICS_LIMIT_SECONDS, panel
