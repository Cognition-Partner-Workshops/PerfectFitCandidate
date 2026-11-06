"""Signed package integrity, replay and hostile ZIP checks (REQ-OCC-01..03)."""

import hashlib
import io
import json
import stat
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from pfc.security import package_signing as ps

KeyPair = tuple[Ed25519PrivateKey, Ed25519PublicKey]


@pytest.fixture
def keys() -> KeyPair:
    return ps.generate_keypair()


def build(
    private: Ed25519PrivateKey, *, version: int = 2, schema: int = 1, rollback: bool = False
) -> bytes:
    return ps.build_settings_package(
        {f"settings.v{schema}.json": {"synonyms": [], "accounts": []}},
        private=private,
        package_version=version,
        schema_version=schema,
        min_app_version="0.1.0",
        max_app_version="1.0.0",
        created_at="2026-11-06T00:00:00Z",
        rollback=rollback,
    )


def verify(package: bytes, public: Ed25519PublicKey, **kwargs: Any) -> ps.VerifiedPackage:
    return ps.verify_package(
        package, trusted_keys=[public], app_version="0.1.0", current_version=1, **kwargs
    )


def rewrite(
    package: bytes,
    *,
    updates: Mapping[str, bytes] | None = None,
    remove: tuple[str, ...] = (),
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        members = {name: archive.read(name) for name in archive.namelist() if name not in remove}
    members.update(updates or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output.getvalue()


def assert_reason(
    reason: ps.Reason, function: Callable[..., Any], *args: Any, **kwargs: Any
) -> None:
    with pytest.raises(ps.PackageError) as error:
        function(*args, **kwargs)
    assert error.value.reason == reason


def test_roundtrip_and_serialisation(keys: KeyPair) -> None:
    private, public = keys
    password = hashlib.sha256(ps.serialise_public_key(ps.generate_keypair()[1])).digest()
    encrypted = ps.serialise_private_key(private, password)
    assert b"ENCRYPTED PRIVATE KEY" in encrypted
    loaded = ps.load_private_key(encrypted, password)
    assert ps.serialise_public_key(loaded.public_key()) == ps.serialise_public_key(public)
    assert ps.key_id(ps.load_public_key(ps.serialise_public_key(public))) == ps.key_id(public)
    with pytest.raises(ValueError):
        ps.serialise_private_key(private, b"")
    package = build(private)
    result = verify(package, public)
    assert result.manifest.package_version == 2
    assert result.settings_payloads == {"settings.v1.json": {"synonyms": [], "accounts": []}}
    assert result.trusted_key_id == ps.key_id(public)
    assert (
        ps.verify_package(
            package,
            trusted_keys=[ps.generate_keypair()[1], public],
            app_version="1.0.0",
            current_version=0,
        ).manifest.kind
        == "settings"
    )
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        encoded = archive.read(ps.MANIFEST_NAME)
        assert encoded == ps.canonical_json(result.manifest.model_dump())
        ps.verify_signature(encoded, archive.read(ps.SIGNATURE_NAME), public)


def test_tampering_and_wrong_key(keys: KeyPair) -> None:
    private, public = keys
    package = build(private)
    tampered = rewrite(package, updates={"settings.v1.json": b"{}"})
    assert_reason(ps.Reason.PAYLOAD_HASH, verify, tampered, public)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = json.loads(archive.read(ps.MANIFEST_NAME))
    manifest["package_version"] = 3
    assert_reason(
        ps.Reason.INVALID_SIGNATURE,
        verify,
        rewrite(package, updates={ps.MANIFEST_NAME: ps.canonical_json(manifest)}),
        public,
    )
    assert_reason(ps.Reason.UNTRUSTED_KEY, verify, package, ps.generate_keypair()[1])
    assert_reason(
        ps.Reason.INVALID_SIGNATURE,
        verify,
        rewrite(package, updates={ps.SIGNATURE_NAME: bytes(64)}),
        public,
    )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_replay_and_older(keys: KeyPair, version: int) -> None:
    private, public = keys
    assert_reason(
        ps.Reason.STALE_VERSION,
        ps.verify_package,
        build(private, version=version),
        trusted_keys=[public],
        app_version="0.1.0",
        current_version=3,
    )


@pytest.mark.parametrize("app", ["0.0.9", "1.0.1", "not-a-version"])
def test_incompatible_app(keys: KeyPair, app: str) -> None:
    private, public = keys
    assert_reason(
        ps.Reason.INCOMPATIBLE_APP,
        ps.verify_package,
        build(private),
        trusted_keys=[public],
        app_version=app,
        current_version=1,
    )


def test_schema_and_kind(keys: KeyPair) -> None:
    private, public = keys
    assert_reason(ps.Reason.UNSUPPORTED_SCHEMA, verify, build(private, schema=2), public)
    assert_reason(ps.Reason.WRONG_KIND, verify, build(private), public, expected_kind="model")


@pytest.mark.parametrize(
    "remove", [("settings.v1.json",), (ps.MANIFEST_NAME,), (ps.SIGNATURE_NAME,)]
)
def test_missing_members(keys: KeyPair, remove: tuple[str, ...]) -> None:
    private, public = keys
    assert_reason(ps.Reason.MEMBERS, verify, rewrite(build(private), remove=remove), public)


def test_extra_and_duplicate(keys: KeyPair) -> None:
    private, public = keys
    package = build(private)
    assert_reason(
        ps.Reason.MEMBERS, verify, rewrite(package, updates={"extra.json": b"{}"}), public
    )
    output = io.BytesIO(package)
    with zipfile.ZipFile(output, "a") as archive, pytest.warns(UserWarning, match="Duplicate"):
        archive.writestr("settings.v1.json", b"{}")
    assert_reason(ps.Reason.MEMBERS, verify, output.getvalue(), public)


@pytest.mark.parametrize("name", ["../evil", "/absolute", "C:/evil", "..\\evil", "a/../b", "a//b"])
def test_unsafe_paths(keys: KeyPair, name: str) -> None:
    private, public = keys
    assert_reason(
        ps.Reason.UNSAFE_PATH, verify, rewrite(build(private), updates={name: b"x"}), public
    )


def test_limits_and_bomb(keys: KeyPair) -> None:
    private, public = keys
    package = build(private)
    limits = ps.PackageLimits()
    for limited in (
        replace(limits, max_members=2),
        replace(limits, max_member_bytes=1),
        replace(limits, max_total_bytes=1),
        replace(limits, max_manifest_bytes=1),
    ):
        assert_reason(ps.Reason.SIZE_LIMIT, verify, package, public, limits=limited)
    bomb = rewrite(package, updates={"bomb": b"0" * 100_000}, compression=zipfile.ZIP_DEFLATED)
    assert_reason(ps.Reason.SIZE_LIMIT, verify, bomb, public)


def test_model_streaming(keys: KeyPair) -> None:
    private, public = keys

    class BoundedStream(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None and 0 < size <= ps.CHUNK_SIZE
            return super().read(size)

    model = BoundedStream(b"model-artifact" * 100_000)
    output = io.BytesIO()
    manifest = ps.build_model_package(
        {"models/nomic.onnx": model, "tokenizer.json": b"{}"},
        output,
        private=private,
        package_version=2,
        min_app_version="0.1.0",
        max_app_version="1.0.0",
        created_at="2026-11-06T00:00:00Z",
    )
    result = verify(output.getvalue(), public, expected_kind="model")
    assert result.manifest == manifest
    assert result.settings_payloads == {}
    assert (
        manifest.payloads["models/nomic.onnx"].sha256
        == hashlib.sha256(model.getvalue()).hexdigest()
    )
    tampered = rewrite(
        output.getvalue(), updates={"models/nomic.onnx": b"x" * len(model.getvalue())}
    )
    assert_reason(ps.Reason.PAYLOAD_HASH, verify, tampered, public, expected_kind="model")
    assert not model.closed and not output.closed


def test_malformed_and_symlink(keys: KeyPair) -> None:
    private, public = keys
    assert_reason(ps.Reason.INVALID_ARCHIVE, verify, b"not a zip", public)
    package = build(private)
    assert_reason(
        ps.Reason.INVALID_MANIFEST,
        verify,
        rewrite(package, updates={ps.MANIFEST_NAME: b"{}"}),
        public,
    )
    output = io.BytesIO(package)
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(output, "a") as archive:
        archive.writestr(info, "target")
    assert_reason(ps.Reason.INVALID_ARCHIVE, verify, output.getvalue(), public)


def test_invalid_signed_payload(keys: KeyPair) -> None:
    private, public = keys
    package = build(private)
    payload = b'{"duplicate":1,"duplicate":2}'
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = json.loads(archive.read(ps.MANIFEST_NAME))
    manifest["payloads"]["settings.v1.json"] = {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    encoded = ps.canonical_json(manifest)
    tampered = rewrite(
        package,
        updates={
            "settings.v1.json": payload,
            ps.MANIFEST_NAME: encoded,
            ps.SIGNATURE_NAME: ps.sign(encoded, private),
        },
    )
    assert_reason(ps.Reason.INVALID_PAYLOAD, verify, tampered, public)


def test_invalid_build_arguments(keys: KeyPair) -> None:
    private, _ = keys
    with pytest.raises(ps.PackageError):
        build(private, version=0)
    with pytest.raises(ValueError):
        ps.PackageLimits(max_members=0)
    assert_reason(
        ps.Reason.INVALID_MANIFEST,
        ps.build_settings_package,
        {"unversioned.json": {}},
        private=private,
        package_version=1,
        min_app_version="1.0",
        max_app_version="2.0",
        created_at="2026-11-06T00:00:00Z",
    )


def test_corrupt_compressed_member(keys: KeyPair) -> None:
    private, public = keys
    compressed = rewrite(build(private), compression=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(io.BytesIO(compressed)) as archive:
        info = archive.getinfo("settings.v1.json")
    damaged = bytearray(compressed)
    offset = info.header_offset
    filename_size = int.from_bytes(damaged[offset + 26 : offset + 28], "little")
    extra_size = int.from_bytes(damaged[offset + 28 : offset + 30], "little")
    data_start = offset + 30 + filename_size + extra_size
    damaged[data_start : data_start + info.compress_size] = bytes([255]) * info.compress_size
    assert_reason(ps.Reason.INVALID_ARCHIVE, verify, bytes(damaged), public)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "marked,allowed", [(False, False), (False, True), (True, False), (True, True)]
)
def test_signed_rollback_requires_marker_and_permission(
    keys: KeyPair, version: int, marked: bool, allowed: bool
) -> None:
    """Only explicit authorization AND Admin-signed intent bypass monotonicity (REQ-OCC-02)."""
    private, public = keys
    package = build(private, version=version, rollback=marked)
    if marked and allowed:
        result = ps.verify_package(
            package,
            trusted_keys=[public],
            app_version="0.1.0",
            current_version=2,
            allow_rollback=allowed,
        )
        assert result.manifest.rollback
        assert result.manifest.package_version == version
    else:
        assert_reason(
            ps.Reason.STALE_VERSION,
            ps.verify_package,
            package,
            trusted_keys=[public],
            app_version="0.1.0",
            current_version=2,
            allow_rollback=allowed,
        )


def test_rollback_cannot_bypass_trust_integrity_or_compatibility(keys: KeyPair) -> None:
    private, public = keys
    package = build(private, version=1, rollback=True)
    assert_reason(
        ps.Reason.UNTRUSTED_KEY, verify, package, ps.generate_keypair()[1], allow_rollback=True
    )
    assert_reason(
        ps.Reason.PAYLOAD_HASH,
        verify,
        rewrite(package, updates={"settings.v1.json": b"{}"}),
        public,
        allow_rollback=True,
    )
    assert_reason(
        ps.Reason.INCOMPATIBLE_APP,
        ps.verify_package,
        package,
        trusted_keys=[public],
        app_version="2.0.0",
        current_version=2,
        allow_rollback=True,
    )
    assert_reason(
        ps.Reason.UNSUPPORTED_SCHEMA,
        verify,
        build(private, version=1, schema=2, rollback=True),
        public,
        allow_rollback=True,
    )
    normal = build(private, version=1)
    with zipfile.ZipFile(io.BytesIO(normal)) as archive:
        manifest = json.loads(archive.read(ps.MANIFEST_NAME))
    manifest["rollback"] = True
    assert_reason(
        ps.Reason.INVALID_SIGNATURE,
        verify,
        rewrite(normal, updates={ps.MANIFEST_NAME: ps.canonical_json(manifest)}),
        public,
        allow_rollback=True,
    )


def test_legacy_manifest_without_rollback_marker(keys: KeyPair) -> None:
    private, public = keys
    package = build(private)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = json.loads(archive.read(ps.MANIFEST_NAME))
    del manifest["rollback"]
    encoded = ps.canonical_json(manifest)
    legacy = rewrite(
        package, updates={ps.MANIFEST_NAME: encoded, ps.SIGNATURE_NAME: ps.sign(encoded, private)}
    )
    assert verify(legacy, public).manifest.rollback is False
    assert_reason(
        ps.Reason.STALE_VERSION,
        ps.verify_package,
        legacy,
        trusted_keys=[public],
        app_version="0.1.0",
        current_version=2,
        allow_rollback=True,
    )
