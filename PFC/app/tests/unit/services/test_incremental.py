import json

import pytest

from pfc.services.matching.incremental import plan_run

INPUTS = {"version": 1, "demands": {"1": "a" * 64}, "resources": {"2": "b" * 64}}


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"requested": False}, "requested_full"),
        ({"scope": "demands"}, "scope_not_full"),
        ({"base": None}, "no_base_run"),
        ({"scope_base": "demands"}, "base_scope_not_full"),
        ({"fingerprints": None}, "fingerprints_missing"),
        ({"fingerprints": '{"version":2}'}, "fingerprints_missing"),
        ({"fingerprints": "{"}, "fingerprints_invalid"),
        ({"fingerprints": '{"version":1,"demands":[],"resources":{}}'}, "fingerprints_invalid"),
        (
            {"fingerprints": '{"version":1,"demands":{"1":0},"resources":{}}'},
            "fingerprints_invalid",
        ),
        ({"key": "changed"}, "config_changed"),
        ({"inputs": {**INPUTS, "resources": {}}}, "resource_removed"),
        ({"inputs": {**INPUTS, "demands": {}}}, "demand_removed"),
    ],
)
def test_fallback_reasons(change: dict, reason: str) -> None:
    base = {
        "run_id": 7,
        "scope": change.get("scope_base", "full"),
        "config_key": "key",
        "input_fingerprints_json": change.get("fingerprints", json.dumps(INPUTS)),
    }
    plan = plan_run(
        change.get("requested", True),
        change.get("scope", "full"),
        change.get("key", "key"),
        change.get("inputs", INPUTS),
        change.get("base", base),
    )
    assert plan.mode == "full"
    assert plan.reason == reason
    assert plan.base_run_id is None


def test_clean_new_and_changed_fingerprints() -> None:
    base = {
        "run_id": 7,
        "scope": "full",
        "config_key": "key",
        "input_fingerprints_json": json.dumps(INPUTS),
    }
    clean = plan_run(True, "full", "key", INPUTS, base)
    assert clean.mode == "incremental"
    assert not clean.demands and not clean.resources
    changed = {**INPUTS, "resources": {"2": "c" * 64, "3": "d" * 64}}
    plan = plan_run(True, "full", "key", changed, base)
    assert plan.demands == frozenset()
    assert plan.resources == frozenset({"2", "3"})
    assert plan.base_run_id == 7
