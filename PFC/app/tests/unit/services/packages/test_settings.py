"""Settings roundtrip, OTP secrecy, preview, rollback and atomic failure (REQ-OCC-01/02)."""

import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.db.users import create_user
from pfc.security import package_signing as signing
from pfc.security.passwords import validate_password, verify
from pfc.services.packages.common import PackageServiceError
from pfc.services.packages.export import ExportResult, export_settings
from pfc.services.packages.importer import SettingsImporter
from pfc.services.packages.keys import SigningKeys, load_trusted_keys
from pfc.services.packages.payloads import TABLES, encode_snapshot, snapshot

from .conftest import NOW, initialize, trust


def exported(source: Settings) -> ExportResult:
    return export_settings(source, passphrase="test signing passphrase", actor_id=1)


def verify_export(source: Settings, target: Settings, package: bytes) -> signing.VerifiedPackage:
    return signing.verify_package(
        package, trusted_keys=load_trusted_keys(target), app_version="0.1.0", current_version=0
    )


def rewrite(package: bytes, member: str, contents: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as archive, zipfile.ZipFile(output, "w") as result:
        for info in archive.infolist():
            result.writestr(
                info.filename, contents if info.filename == member else archive.read(info)
            )
    return output.getvalue()


def test_roundtrip_preview_secrecy_and_admin_protection(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    with database(source) as db:
        create_user(
            db,
            email="protected@example.com",
            display_name="Source analyst",
            role_id=2,
            password_hash="source-password",
            now=NOW,
        )
    with database(target) as db:
        create_user(
            db,
            email="protected@example.com",
            display_name="Target admin",
            role_id=1,
            password_hash="target-admin-hash",
            now=NOW,
            must_change_password=False,
        )
        db.execute(
            "INSERT INTO llm_providers(provider_id, name, kind, fallback_order, token_blob, "
            "owner_user_id, updated_at) VALUES (8, 'local', 'llamacpp', 0, ?, 1, ?)",
            (b"target-provider-secret", NOW),
        )
    result = exported(source)
    assert result.version == 1 and result.path.name == "PFC-settings-v1.pfcpkg"
    assert "one_time_passwords" not in repr(result)
    package = result.path.read_bytes()
    verified = verify_export(source, target, package)
    assert len(verified.settings_payloads["llm_models.v1.json"]) == 1
    provider = verified.settings_payloads["llm_providers.v1.json"][0]
    assert "token_blob" not in provider and "owner_user_id" not in provider
    assert b"provider-secret" not in package and b"admin-secret" not in package
    assert b"other-secret" not in package
    imported_account = verified.settings_payloads["analysts.v1.json"][0]
    assert imported_account["role"] == "Analyst"
    assert imported_account["must_change_password"] is True
    otp = result.one_time_passwords["analyst@example.com"]
    validate_password(otp, "analyst@example.com", "Analyst")
    assert verify(imported_account["password_hash"], otp, source)
    importer = SettingsImporter(target)
    with database(target) as db:
        before = snapshot(db)
        admins = [dict(row) for row in db.execute("SELECT * FROM users WHERE role_id=1")]
    preview = importer.preview(package)
    assert preview.changes["skills"].added == ("1",)
    with database(target) as db:
        assert snapshot(db) == before
        assert db.execute("SELECT count(*) FROM settings_packages").fetchone()[0] == 0
    assert not (target.data_dir / "packages").exists()
    importer.apply(result.path, actor_id=1)
    with database(target) as db:
        after = snapshot(db)
        for table in TABLES:
            assert after[table] == verified.settings_payloads[f"{table}.v1.json"]
        assert [dict(row) for row in db.execute("SELECT * FROM users WHERE role_id=1")] == admins
        assert (
            db.execute("SELECT token_blob FROM llm_providers").fetchone()[0]
            == b"target-provider-secret"
        )
        assert db.execute("SELECT occ_reviewed_at FROM demands").fetchone()[0] == NOW
        assert db.execute("SELECT occ_reviewed_at FROM resources").fetchone()[0] == NOW
        row = db.execute("SELECT * FROM users WHERE email='analyst@example.com'").fetchone()
        assert row["must_change_password"] == 1 and verify(row["password_hash"], otp, target)
        assert verify_chain(db)
    assert (target.data_dir / "packages/v1.pfcpkg").read_bytes() == package
    with database(source) as db:
        assert (
            db.execute("SELECT password_hash FROM users WHERE user_id=2").fetchone()[0]
            == "local-analyst-hash"
        )
        assert verify_chain(db)
    for password in result.one_time_passwords.values():
        for settings in (source, target):
            for path in settings.data_dir.rglob("*"):
                if path.is_file():
                    assert password.encode() not in path.read_bytes(), path


@pytest.mark.parametrize(
    "failure", ["signature", "stale", "wrong_kind", "incompatible", "untrusted"]
)
def test_rejected_packages(pair: tuple[Settings, Settings], failure: str) -> None:
    source, target = pair
    result = exported(source)
    package = result.path.read_bytes()
    expected = signing.Reason.INVALID_SIGNATURE
    if failure == "signature":
        package = rewrite(package, signing.SIGNATURE_NAME, bytes(64))
    elif failure == "stale":
        SettingsImporter(target).apply(package, actor_id=1)
        expected = signing.Reason.STALE_VERSION
    elif failure == "wrong_kind":
        output = io.BytesIO()
        signing.build_model_package(
            {"model.onnx": b"model"},
            output,
            private=SigningKeys(source).get_or_create("test signing passphrase"),
            package_version=2,
            min_app_version="0.1.0",
            max_app_version="0.1.0",
            created_at=NOW,
        )
        package = output.getvalue()
        expected = signing.Reason.WRONG_KIND
    elif failure == "incompatible":
        package = export_settings(
            source,
            passphrase="test signing passphrase",
            actor_id=1,
            min_app_version="9.0",
            max_app_version="9.0",
        ).path.read_bytes()
        expected = signing.Reason.INCOMPATIBLE_APP
    else:
        (target.data_dir / "keys/trusted/admin.pub").unlink()
        expected = signing.Reason.UNTRUSTED_KEY
    with pytest.raises(signing.PackageError) as error:
        SettingsImporter(target).apply(package, actor_id=1)
    assert error.value.reason == expected


def test_atomic_apply_and_rollback(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    importer = SettingsImporter(target)
    first = exported(source)
    importer.apply(first.path, actor_id=1)
    with database(source) as db:
        db.execute("UPDATE skills SET name='Rust' WHERE skill_id=1")
        db.execute("INSERT INTO skills VALUES (2, 'Go', NULL, 'admin', 1)")
        db.execute("DELETE FROM resource_team_recipients")
        db.execute("UPDATE demands SET occ_reviewed_at=NULL, occ_reviewed_by=NULL")
    second = exported(source)
    preview = importer.preview(second.path)
    assert preview.changes["skills"].added == ("2",)
    assert preview.changes["skills"].changed == ("1",)
    assert preview.changes["resource_team_recipients"].removed == ("1",)
    with database(target) as db:
        baseline = snapshot(db)
        events = db.execute("SELECT count(*) FROM audit_log").fetchone()[0]

    def fail() -> None:
        raise RuntimeError("mid-apply failure")

    broken = SettingsImporter(target, failure_hook=fail)
    with pytest.raises(RuntimeError, match="mid-apply"):
        broken.apply(second.path, actor_id=1)
    assert not (target.data_dir / "packages/v2.pfcpkg").exists()
    with database(target) as db:
        assert snapshot(db) == baseline
        assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == events
        assert (
            db.execute(
                "SELECT package_version FROM settings_packages WHERE is_active=1"
            ).fetchone()[0]
            == 1
        )
    importer.apply(second.path, actor_id=1)
    with database(target) as db:
        assert (
            db.execute(
                "SELECT previous_package_id FROM settings_packages WHERE is_active=1"
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT occ_reviewed_at FROM demands").fetchone()[0] is None
        v2 = snapshot(db)
    with pytest.raises(RuntimeError, match="mid-apply"):
        broken.rollback(actor_id=1)
    with database(target) as db:
        assert snapshot(db) == v2
        assert (
            db.execute(
                "SELECT package_version FROM settings_packages WHERE is_active=1"
            ).fetchone()[0]
            == 2
        )
    assert importer.rollback(actor_id=1).manifest.package_version == 1
    with database(target) as db:
        restored = snapshot(db)
        assert restored.pop("analysts") == v2["analysts"]
        assert restored == {key: value for key, value in baseline.items() if key != "analysts"}
        assert (
            db.execute("SELECT action FROM audit_log ORDER BY audit_id DESC LIMIT 1").fetchone()[0]
            == "settings_package.rollback"
        )
        assert verify_chain(db)
    assert (target.data_dir / "packages/v2.pfcpkg").exists()
    with pytest.raises(PackageServiceError, match="No previous"):
        importer.rollback(actor_id=1)


@pytest.mark.parametrize("tamper", ["retained_file", "revoked_key"])
def test_rollback_reverifies_retained_package(pair: tuple[Settings, Settings], tamper: str) -> None:
    source, target = pair
    importer = SettingsImporter(target)
    importer.apply(exported(source).path, actor_id=1)
    importer.apply(exported(source).path, actor_id=1)
    if tamper == "retained_file":
        path = target.data_dir / "packages/v1.pfcpkg"
        path.write_bytes(rewrite(path.read_bytes(), signing.SIGNATURE_NAME, bytes(64)))
    else:
        (target.data_dir / "keys/trusted/admin.pub").unlink()
    with pytest.raises((PackageServiceError, signing.PackageError)):
        importer.rollback(actor_id=1)
    with database(target) as db:
        assert (
            db.execute(
                "SELECT package_version FROM settings_packages WHERE is_active=1"
            ).fetchone()[0]
            == 2
        )


def test_empty_configuration_and_no_previous(tmp_path: Path) -> None:
    source, target = initialize(tmp_path / "source"), initialize(tmp_path / "target")
    with database(source) as db:
        db.execute("UPDATE users SET is_active=0 WHERE role_id=2")
    trust(source, target)
    result = exported(source)
    assert result.one_time_passwords == {}
    importer = SettingsImporter(target)
    with pytest.raises(PackageServiceError, match="No previous"):
        importer.rollback(actor_id=1)
    importer.apply(result.path, actor_id=1)
    with database(target) as db:
        assert db.execute("SELECT count(*) FROM skills").fetchone()[0] == 0


def test_export_monotonic_active_version_and_concurrent_calls(
    pair: tuple[Settings, Settings],
) -> None:
    source, target = pair
    first = exported(source)
    SettingsImporter(target).apply(first.path, actor_id=1)
    assert exported(target).version == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = sorted(
            result.version for result in pool.map(lambda _: exported(source), range(2))
        )
    assert versions == [2, 3]
    with database(source) as db:
        assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 3


def test_deferred_foreign_key_failure_is_atomic(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    importer = SettingsImporter(target)
    importer.apply(exported(source).path, actor_id=1)
    with database(target) as db:
        db.execute(
            "INSERT INTO resource_skills(resource_id, source, skill_raw, skill_norm, "
            "canonical_skill_id) VALUES (1, 'CT', 'Python', 'python', 1)"
        )
        before = snapshot(db)
    with database(source) as db:
        db.execute("DELETE FROM skill_synonyms")
        db.execute("DELETE FROM skills")
    package = exported(source)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        importer.apply(package.path, actor_id=1)
    with database(target) as db:
        assert snapshot(db) == before
        assert db.execute("SELECT count(*) FROM settings_packages").fetchone()[0] == 1
    assert not (target.data_dir / "packages/v2.pfcpkg").exists()


def test_payload_validation_and_missing_approval_target(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    with database(source) as db:
        data = snapshot(db)
    data["analysts"] = []
    payloads = encode_snapshot(data)
    payloads["unknown.v1.json"] = []
    private = SigningKeys(source).get_or_create("test signing passphrase")
    package = signing.build_settings_package(
        payloads,
        private=private,
        package_version=1,
        min_app_version="0.1.0",
        max_app_version="0.1.0",
        created_at=NOW,
    )
    with pytest.raises(PackageServiceError, match="payload set"):
        SettingsImporter(target).preview(package)
    with database(target) as db:
        db.execute("DELETE FROM demands")
    package = exported(source)
    with pytest.raises(PackageServiceError) as error:
        SettingsImporter(target).apply(package.path, actor_id=1)
    assert error.value.reason == "approval_target_missing"
    assert not (target.data_dir / "packages/v1.pfcpkg").exists()


def test_provider_credential_urls_are_not_exported(pair: tuple[Settings, Settings]) -> None:
    source, _ = pair
    with database(source) as db:
        db.execute(
            "UPDATE llm_providers SET base_url='https://user:password@example.com?token=secret'"
        )
    with pytest.raises(PackageServiceError) as error:
        exported(source)
    assert error.value.reason == "secret_provider_url"


def test_provider_capability_secrets_are_excluded(pair: tuple[Settings, Settings]) -> None:
    source, target = pair
    with database(source) as db:
        db.execute(
            "UPDATE llm_providers SET capabilities_json=?",
            (
                json.dumps(
                    {
                        "generate": True,
                        "api_key": "hidden-api-key",
                        "nested": {"token": "hidden-token"},
                    }
                ),
            ),
        )
    result = exported(source)
    assert b"hidden-api-key" not in result.path.read_bytes()
    assert b"hidden-token" not in result.path.read_bytes()
    SettingsImporter(target).apply(result.path, actor_id=1)
    with database(target) as db:
        capabilities = json.loads(
            db.execute("SELECT capabilities_json FROM llm_providers").fetchone()[0]
        )
        assert capabilities == {"generate": True, "nested": {}}
