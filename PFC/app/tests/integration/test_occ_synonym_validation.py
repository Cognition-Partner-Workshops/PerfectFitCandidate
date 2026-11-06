import logging

import pytest
from test_matching_api import Api, problem
from test_matching_api import api as api
from test_matching_service import NOW, seed_demand, seed_resource
from test_occ_domain_config import edit, run

from pfc.db.connection import database
from pfc.services.ingestion.repository import domain_config
from pfc.services.occ.domain import occ_domain_config

pytestmark = pytest.mark.parametrize("api", ["Admin"], indirect=True)


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("relation", ["equivalent", "abbreviation", "misspelling"])
def test_synonym_save_rejects_partial_equivalents(api: Api, method: str, relation: str) -> None:
    body = {"canonical": "Python", "term": "pyalias", "relation": relation, "credit": 1}
    path = "/api/v1/occ/synonyms"
    if method == "PUT":
        saved = edit(api, "synonyms", body, "POST")
        path += f"/{saved['synonym_id']}"
    with database(api[0].app.state.settings) as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM skill_synonyms")]
        audits = db.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    response = api[0].request(method, path, headers=api[1], json={**body, "credit": 0.5})
    problem(response, 422, "VALIDATION_ERROR")
    with database(api[0].app.state.settings) as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM skill_synonyms")] == before
        assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == audits
        assert occ_domain_config(db).synonyms.resolve("Python") == "Python"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_synonym_transition_validates_legacy_row(api: Api, action: str) -> None:
    saved = edit(
        api,
        "synonyms",
        {
            "canonical": "Python",
            "term": "pyalias",
            "relation": "equivalent",
            "credit": 1,
            "status": "suggested",
        },
        "POST",
    )
    target = saved["synonym_id"]
    with database(api[0].app.state.settings) as db:
        db.execute("UPDATE skill_synonyms SET credit=0.5 WHERE synonym_id=?", (target,))
    response = api[0].post(f"/api/v1/occ/synonyms/{target}/{action}", headers=api[1], json={})
    problem(response, 422, "VALIDATION_ERROR")
    with database(api[0].app.state.settings) as db:
        assert (
            db.execute(
                "SELECT status FROM skill_synonyms WHERE synonym_id=?", (target,)
            ).fetchone()[0]
            == "suggested"
        )


@pytest.mark.parametrize(
    "body",
    [
        {"canonical": "!!!", "term": "validalias"},
        {"canonical": "Python", "term": "!!!"},
        {"canonical": "Python", "term": "SQL"},
    ],
)
def test_synonym_save_validates_domain_table(api: Api, body: dict[str, str]) -> None:
    response = api[0].post(
        "/api/v1/occ/synonyms", headers=api[1], json={**body, "relation": "equivalent", "credit": 1}
    )
    problem(response, 422, "VALIDATION_ERROR")


@pytest.mark.parametrize("invalid", ["credit", "ambiguous"])
def test_builder_skips_invalid_stored_row(
    api: Api, caplog: pytest.LogCaptureFixture, invalid: str
) -> None:
    saved = edit(
        api,
        "synonyms",
        {"canonical": "Python", "term": "pyalias", "relation": "equivalent", "credit": 1},
        "POST",
    )
    with database(api[0].app.state.settings) as db:
        if invalid == "credit":
            db.execute(
                "UPDATE skill_synonyms SET credit=0.5 WHERE synonym_id=?", (saved["synonym_id"],)
            )
        else:
            db.execute(
                "UPDATE skill_synonyms SET term='SQL' WHERE synonym_id=?", (saved["synonym_id"],)
            )
        changes = db.total_changes
        with caplog.at_level(logging.WARNING):
            config = occ_domain_config(db)
        assert db.total_changes == changes
        assert config.synonyms.resolve("pyalias") is None
        assert config.synonyms.resolve("SQL") == "SQL"
        assert config.synonyms.resolve("Python") == "Python"
        assert (
            str(saved["synonym_id"]) in caplog.text
            and "Skipping invalid stored synonym" in caplog.text
        )
        assert domain_config(db).synonyms.rows == config.synonyms.rows
        seed_demand(db)
        seed_resource(db)
    run_id = run(api)
    assert api[0].get(f"/api/v1/analytics/match?runId={run_id}").status_code == 200
    assert api[0].post("/api/v1/demands/1/analyze", headers=api[1]).status_code == 200


@pytest.mark.parametrize("alias_first", [True, False])
def test_canonical_identity_survives_earlier_conflicting_alias(
    api: Api, caplog: pytest.LogCaptureFixture, alias_first: bool
) -> None:
    rows = [
        ("Python", "SQL", "sql"),
        ("SQL", "Structured Query Language", "structured query language"),
    ]
    with database(api[0].app.state.settings) as db:
        db.execute("DELETE FROM skill_synonyms")
        for canonical, term, norm in rows if alias_first else reversed(rows):
            db.execute(
                "INSERT INTO skill_synonyms(skill_id,term,term_norm,relation,credit,status,"
                "updated_at) SELECT skill_id,?,?,'equivalent',1,'approved',? FROM skills "
                "WHERE name=?",
                (term, norm, NOW.isoformat(), canonical),
            )
        invalid_id = db.execute(
            "SELECT synonym_id FROM skill_synonyms WHERE term='SQL'"
        ).fetchone()[0]
        changes = db.total_changes
        with caplog.at_level(logging.WARNING):
            synonyms = occ_domain_config(db).synonyms
        assert db.total_changes == changes
        assert synonyms.resolve("SQL") == "SQL"
        assert synonyms.resolve("Structured Query Language") == "SQL"
        assert synonyms.resolve("Python") == "Python"
        assert not any(row.canonical == "Python" and row.term == "SQL" for row in synonyms.rows)
        assert f"Skipping invalid stored synonym row {invalid_id}" in caplog.text


def test_valid_synonyms_work_with_empty_or_invalid_store(api: Api) -> None:
    with database(api[0].app.state.settings) as db:
        db.execute("DELETE FROM skill_synonyms")
    assert (
        edit(
            api,
            "synonyms",
            {"canonical": "Python", "term": "pyalias", "relation": "equivalent", "credit": 1},
            "POST",
        )["credit"]
        == 1
    )
    with database(api[0].app.state.settings) as db:
        db.execute("UPDATE skill_synonyms SET credit=0.5")
    saved = edit(
        api,
        "synonyms",
        {"canonical": "Python", "term": "sqlish", "relation": "related", "credit": 0.6},
        "POST",
    )
    with database(api[0].app.state.settings) as db:
        assert occ_domain_config(db).synonyms.related("sqlish") == [("Python", 0.6)]
    edit(
        api,
        f"synonyms/{saved['synonym_id']}",
        {"canonical": "Python", "term": "sqlish", "relation": "related", "credit": 0.4},
    )
