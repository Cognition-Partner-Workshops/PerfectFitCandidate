"""REQ-SCR-01, REQ-ELIG-07, REQ-GRD-01 shipped configuration contract."""

import json
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from pfc.domain.config_loader import ScoringTemplate, load_domain_config

CONFIG = Path(__file__).resolve().parents[3] / "config"


def test_req_scr_01_shipped_defaults():
    config = load_domain_config()
    assert config.grades.codes == ("A3", "A4", "B1", "B2", "C1", "C2", "D1", "D2", "E1")
    template = config.template
    assert template.name == "default"
    assert template.version == 1
    assert template.weights.model_dump() == dict(
        skills=70, grade=10, location=5, bench_start=5, bench_end=5, status=5
    )
    assert template.skills.sub_weights.model_dump() == dict(
        mandatory=0.6, preferred=0.15, semantic=0.25
    )
    assert template.bench_start.tiers_days == ((0, 5), (30, 4), (45, 3), (60, 2), (90, 1))
    assert template.bench_start.missing == template.bench_start.beyond == 0
    assert template.bench_end_blank_points == 5
    assert template.unmapped_status_priority == 2
    assert template.status_score_mode == "binary"
    assert template.status_tiered_points == {1: 5, 2: 4, 3: 3, 4: 2, 5: 1, 6: 0}
    assert template.bands == {"Strong": 90, "Potential": 70, "Avg": 61}
    assert template.weak_inclusion.min_transferable_semantic is None
    assert template.top_n == 5 and template.email_preselect_n == 4
    assert template.mandatory_gap_policy == "flag_only"


@pytest.mark.parametrize(
    ("first", "second", "distance"),
    [("D1", "C2", 1), ("C1", "B2", 1), ("A3", "E1", 8), ("C2", "C2", 0)],
)
def test_req_grd_01_distance(first, second, distance):
    grades = load_domain_config().grades
    assert grades.ordinal("A3") == 1
    assert grades.distance(first, second) == distance
    with pytest.raises(ValueError):
        grades.ordinal("invalid")


@pytest.mark.parametrize(
    "label", ["Selected", "Unknown", "CV with Client - Sold - Pending Allocation"]
)
def test_req_elig_07_exclusions(label):
    status = load_domain_config().bench_statuses.resolve(label)
    assert status.is_excluded and status.priority is None


def test_req_elig_07_statuses():
    statuses = load_domain_config().bench_statuses
    assert len(statuses.statuses) == len(statuses.status_map) == 10
    assert statuses.resolve(" available ").priority == 1
    assert statuses.resolve("Overall upskilling (New bench)").priority == 2
    assert statuses.resolve("CV with client - Demand Proposed").flag_in_gaps
    assert statuses.resolve("CV with client - Proactive Proposed").flag_in_gaps
    assert statuses.resolve("Expected on Bench Soon").available_from_bench_start
    assert statuses.resolve("never mapped") is None


@pytest.mark.parametrize(
    ("term", "canonical"),
    [
        ("BA", "Business Analysis"),
        ("Business Analyst", "Business Analysis"),
        ("PM", "Project Management"),
        ("Project Manager", "Project Management"),
        ("DA", "Data Analysis"),
        ("Data Analyst", "Data Analysis"),
        ("DataBricks", "Databricks"),
        ("Generative AI", "GenAI"),
        ("AI", "GenAI"),
        ("data mapping", "Data migration"),
        ("reconciliation", "Data migration"),
        ("profiling", "Data migration"),
        ("data quality", "Data migration"),
        ("Bussiness Analysis", "Business Analysis"),
        ("Pyspark", "Spark"),
        ("PySpark", "Spark"),
        ("SQL", "SQL"),
        ("Python", "Python"),
        ("AWS", "AWS"),
        ("Azure", "Azure"),
    ],
)
def test_req_mat_05_seed(term, canonical):
    assert load_domain_config().synonyms.resolve(term) == canonical


def test_req_mat_05_related_seed():
    synonyms = load_domain_config().synonyms
    assert ("Delivery Lead", 0.6) in synonyms.related("Project Manager")
    assert ("Delivery Lead", 0.6) in synonyms.related("Program Manager")
    assert synonyms.resolve("Project Manager") == "Project Management"


def test_req_loc_01_clusters():
    locations = load_domain_config().locations
    assert all(
        locations.aliases[key] == "remote"
        for key in ("remote", "any", "uk remote", "wfh", "anywhere")
    )
    assert locations.travel_clusters["uk-north-west"] == ("manchester", "liverpool")
    assert locations.travel_clusters["scotland"] == ("edinburgh", "glasgow")
    assert not any(
        "manchester" in cluster and ("london" in cluster or "northampton" in cluster)
        for cluster in locations.travel_clusters.values()
    )


def test_req_scr_01_immutable():
    config = load_domain_config()
    with pytest.raises(ValidationError, match="frozen_instance"):
        config.template.version = 2
    with pytest.raises(TypeError):
        config.template.bands["Strong"] = 1
    with pytest.raises(TypeError):
        config.locations.aliases["x"] = "y"


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("weights", "skills", 69, "sum to 100"),
        ("weights", "skills", float("nan"), "finite"),
        (
            "skills",
            "sub_weights",
            {"mandatory": 0.5, "preferred": 0.2, "semantic": 0.2},
            "sum to 1.0",
        ),
        ("bands", "Potential", 95, "bands"),
        (None, "status_score_mode", "unknown", "status_score_mode"),
        (None, "mandatory_gap_policy", "reject", "mandatory_gap_policy"),
        (None, "version", 0, "version"),
        (None, "grade", {}, "Invalid scoring template"),
    ],
)
def test_req_scr_01_invalid_template(section, key, value, message):
    data = json.loads((CONFIG / "scoring_template.default.json").read_text())
    target = data[section] if section else data
    target[key] = value
    with pytest.raises(ValueError, match=message):
        ScoringTemplate.from_dict(data)


@pytest.mark.parametrize("data", [{}, [], None])
def test_req_scr_01_empty_template(data):
    with pytest.raises(ValueError, match="Invalid scoring template"):
        ScoringTemplate.from_dict(data)


@pytest.mark.parametrize(
    ("section", "key", "maximum"),
    [
        ("grade", "exact", 10),
        ("grade", "adjacent", 10),
        ("location_points", "Aligned", 5),
        ("location_points", "Remote/unspecified", 5),
        ("location_points", "Potential travel", 5),
        ("location_points", "Location check", 5),
        ("bench_start", "beyond", 5),
        ("bench_start", "missing", 5),
        (None, "bench_end_blank_points", 5),
        *(("status_tiered_points", str(priority), 5) for priority in range(1, 7)),
    ],
)
@pytest.mark.parametrize("bad", [-0.1, 0.1, float("nan"), float("inf")])
def test_req_scr_01_component_point_limits(section, key, maximum, bad):
    data = json.loads((CONFIG / "scoring_template.default.json").read_text())
    target = data[section] if section else data
    target[key] = maximum + bad if bad == 0.1 else bad
    with pytest.raises(ValueError):
        ScoringTemplate.from_dict(data)


@pytest.mark.parametrize("bad", [-1, 5.1, float("nan"), float("inf")])
@pytest.mark.parametrize("tier", range(5))
def test_req_scr_01_bench_tier_point_limits(bad, tier):
    data = json.loads((CONFIG / "scoring_template.default.json").read_text())
    data["bench_start"]["tiers_days"][tier][1] = bad
    with pytest.raises(ValueError):
        ScoringTemplate.from_dict(data)


@pytest.mark.parametrize("value", [0, 5])
def test_req_scr_01_component_point_limits_inclusive(value):
    data = json.loads((CONFIG / "scoring_template.default.json").read_text())
    data["grade"] = {"exact": value, "adjacent": value, "max_distance": 1}
    data["location_points"] = {key: value for key in data["location_points"]}
    data["bench_start"] = {"tiers_days": [[0, value]], "beyond": value, "missing": value}
    data["bench_end_blank_points"] = value
    data["status_tiered_points"] = {key: value for key in data["status_tiered_points"]}
    assert ScoringTemplate.from_dict(data).bench_end_blank_points == value


def test_req_scr_01_pydantic_models_frozen_forbid_extras():
    config = load_domain_config()
    template = config.template
    models = [
        config,
        config.grades,
        config.bench_statuses,
        config.locations,
        *config.bench_statuses.statuses.values(),
        template,
        template.weights,
        template.grade,
        template.skills,
        template.skills.sub_weights,
        template.bench_start,
        template.weak_inclusion,
    ]
    for model in models:
        assert isinstance(model, BaseModel)
        assert model.model_config["frozen"]
        assert model.model_config["extra"] == "forbid"
        values = {name: getattr(model, name) for name in type(model).model_fields}
        with pytest.raises(ValidationError, match="extra_forbidden"):
            type(model).model_validate({**values, "unexpected": 1})
        field = next(iter(type(model).model_fields))
        with pytest.raises(ValidationError, match="frozen_instance"):
            setattr(model, field, getattr(model, field))


@pytest.mark.parametrize(
    "section",
    [None, "weights", "grade", "skills", "bench_start", "weak_inclusion"],
)
def test_req_scr_01_reject_unknown_template_fields(section):
    data = json.loads((CONFIG / "scoring_template.default.json").read_text())
    target = data[section] if section else data
    target["unexpected"] = 1
    with pytest.raises(ValueError, match="extra_forbidden"):
        ScoringTemplate.from_dict(data)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("grade", "exact", "invalid"),
        ("skills", "semantic_low", "invalid"),
        ("skills", "related_synonym_credit", -0.1),
        ("skills", "related_synonym_credit", 1.1),
        ("skills", "semantic_item_cap", 1.1),
        (None, "version", True),
        (None, "version", 1.5),
        (None, "version", "1"),
    ],
)
def test_req_scr_01_pydantic_field_validation(section, key, value):
    data = json.loads((CONFIG / "scoring_template.default.json").read_text())
    target = data[section] if section else data
    target[key] = value
    with pytest.raises(ValueError):
        ScoringTemplate.from_dict(data)


def test_req_scr_01_pydantic_roundtrip_and_mapping_immutability(tmp_path):
    template = load_domain_config().template
    assert ScoringTemplate.from_dict(template.model_dump()) == template
    path = tmp_path / "template.json"
    path.write_text(template.model_dump_json(), encoding="utf-8")
    assert ScoringTemplate.from_json_file(path) == template
    data = template.model_dump()
    loaded = ScoringTemplate.from_dict(data)
    data["bands"]["Strong"] = 95
    assert loaded.bands["Strong"] == 90
    with pytest.raises(TypeError):
        loaded.status_tiered_points[1] = 0
    config = load_domain_config()
    with pytest.raises(TypeError):
        config.bench_statuses.status_map["available"] = "UNKNOWN"
    with pytest.raises(TypeError):
        config.locations.travel_clusters["scotland"] = ()


@pytest.mark.parametrize("codes", [[], ["C1", "C1"]])
def test_req_grd_01_pydantic_grade_validation(codes):
    config = load_domain_config()
    with pytest.raises(ValueError, match="nonempty and unique"):
        type(config.grades)(codes=codes)


def test_req_scr_01_paths_and_overrides(tmp_path, monkeypatch):
    copied = tmp_path / "settings"
    shutil.copytree(CONFIG, copied)
    monkeypatch.setenv("PFC_CONFIG_DIR", str(copied))
    assert load_domain_config().template.version == 1
    assert load_domain_config(CONFIG).template.version == 1
    (copied / "scoring_template.default.json").write_text("{broken")
    with pytest.raises(ValueError, match="JSON"):
        load_domain_config()
    (copied / "status_map.yaml").write_text("Available: NONEXISTENT")
    with pytest.raises(ValueError, match="unknown bench status"):
        load_domain_config(copied)
