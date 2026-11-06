"""Pure signed ZIP creation/validation, without extraction or apply (REQ-OCC-01..03)."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, BinaryIO, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "signature.ed25519"
CHUNK_SIZE = 1024 * 1024
PackageKind = Literal["settings", "model"]


class Reason(StrEnum):
    INVALID_ARCHIVE = "invalid_archive"
    UNSAFE_PATH = "unsafe_path"
    SIZE_LIMIT = "size_limit"
    MEMBERS = "members"
    INVALID_MANIFEST = "invalid_manifest"
    UNTRUSTED_KEY = "untrusted_key"
    INVALID_SIGNATURE = "invalid_signature"
    PAYLOAD_HASH = "payload_hash"
    INVALID_PAYLOAD = "invalid_payload"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INCOMPATIBLE_APP = "incompatible_app"
    STALE_VERSION = "stale_version"
    WRONG_KIND = "wrong_kind"


class PackageError(ValueError):
    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class PayloadDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: int = Field(ge=1)
    package_version: int = Field(ge=1)
    kind: PackageKind
    min_app_version: str
    max_app_version: str
    created_at: str
    key_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    payloads: dict[str, PayloadDigest] = Field(min_length=1)


@dataclass(frozen=True)
class PackageLimits:
    max_members: int = 128
    max_member_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_manifest_bytes: int = 1024 * 1024
    max_compression_ratio: float = 200

    def __post_init__(self) -> None:
        if (
            min(
                self.max_members,
                self.max_member_bytes,
                self.max_total_bytes,
                self.max_manifest_bytes,
                self.max_compression_ratio,
            )
            <= 0
        ):
            raise ValueError("Package limits must be positive")


MODEL_LIMITS = PackageLimits(max_member_bytes=3 * 1024**3, max_total_bytes=4 * 1024**3)


@dataclass(frozen=True)
class VerifiedPackage:
    manifest: Manifest
    settings_payloads: dict[str, Any]
    trusted_key_id: str


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def serialise_private_key(private: Ed25519PrivateKey, password: bytes) -> bytes:
    """Encrypted PKCS8 PEM; platform key protection belongs to caller (REQ-OCC-01)."""
    if not password:
        raise ValueError("A nonempty key-encryption password is required")
    return private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password),
    )


def load_private_key(pem: bytes, password: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem, password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Expected an Ed25519 private key")
    return key


def serialise_public_key(public: Ed25519PublicKey) -> bytes:
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(raw)


def key_id(public: Ed25519PublicKey) -> str:
    return hashlib.sha256(serialise_public_key(public)).hexdigest()


def sign(data: bytes, private: Ed25519PrivateKey) -> bytes:
    return private.sign(data)


def verify_signature(data: bytes, signature: bytes, public: Ed25519PublicKey) -> None:
    try:
        public.verify(signature, data)
    except InvalidSignature as exc:
        raise PackageError(Reason.INVALID_SIGNATURE, "Ed25519 signature is invalid") from exc


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _safe_name(name: str) -> None:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise PackageError(Reason.UNSAFE_PATH, "Unsafe archive member path")


def _json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _decode_json(data: bytes) -> Any:
    return json.loads(data, object_pairs_hook=_json_no_duplicates, parse_constant=_reject_constant)


def _validate_manifest(manifest: Manifest) -> None:
    try:
        minimum, maximum = Version(manifest.min_app_version), Version(manifest.max_app_version)
        timestamp = datetime.fromisoformat(manifest.created_at)
        if minimum > maximum or timestamp.tzinfo is None:
            raise ValueError("Invalid version range or timestamp")
    except (ValueError, InvalidVersion) as exc:
        raise PackageError(
            Reason.INVALID_MANIFEST, "Invalid compatibility range or created_at"
        ) from exc
    for name in manifest.payloads:
        _safe_name(name)
        if name in (MANIFEST_NAME, SIGNATURE_NAME):
            raise PackageError(Reason.INVALID_MANIFEST, "Reserved payload filename")
        if manifest.kind == "settings" and not name.endswith(f".v{manifest.schema_version}.json"):
            raise PackageError(
                Reason.INVALID_MANIFEST, "Settings payloads must have versioned JSON names"
            )


def _build(
    output: BinaryIO,
    payloads: Mapping[str, bytes | BinaryIO],
    *,
    kind: PackageKind,
    private: Ed25519PrivateKey,
    package_version: int,
    schema_version: int,
    min_app_version: str,
    max_app_version: str,
    created_at: str,
) -> Manifest:
    for name in payloads:
        _safe_name(name)
        if name in (MANIFEST_NAME, SIGNATURE_NAME):
            raise PackageError(Reason.MEMBERS, "Reserved payload filename")
    digests: dict[str, PayloadDigest] = {}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, payload in payloads.items():
            stream = io.BytesIO(payload) if isinstance(payload, bytes) else payload
            digest, size = hashlib.sha256(), 0
            with archive.open(name, "w", force_zip64=True) as member:
                while block := stream.read(CHUNK_SIZE):
                    digest.update(block)
                    size += len(block)
                    member.write(block)
            digests[name] = PayloadDigest(sha256=digest.hexdigest(), size=size)
        try:
            manifest = Manifest(
                schema_version=schema_version,
                package_version=package_version,
                kind=kind,
                min_app_version=min_app_version,
                max_app_version=max_app_version,
                created_at=created_at,
                key_id=key_id(private.public_key()),
                payloads=digests,
            )
        except ValidationError as exc:
            raise PackageError(Reason.INVALID_MANIFEST, "Invalid manifest fields") from exc
        _validate_manifest(manifest)
        encoded = canonical_json(manifest.model_dump())
        archive.writestr(MANIFEST_NAME, encoded)
        archive.writestr(SIGNATURE_NAME, sign(encoded, private))
    return manifest


def build_settings_package(
    payloads: Mapping[str, Any],
    *,
    private: Ed25519PrivateKey,
    package_version: int,
    min_app_version: str,
    max_app_version: str,
    created_at: str,
    schema_version: int = 1,
) -> bytes:
    """Return PFC-settings-v<N>.pfcpkg bytes; account payloads stay opaque (REQ-OCC-01)."""
    output = io.BytesIO()
    _build(
        output,
        {name: canonical_json(value) for name, value in payloads.items()},
        kind="settings",
        private=private,
        package_version=package_version,
        schema_version=schema_version,
        min_app_version=min_app_version,
        max_app_version=max_app_version,
        created_at=created_at,
    )
    return output.getvalue()


def build_model_package(
    payloads: Mapping[str, bytes | BinaryIO],
    output: BinaryIO,
    *,
    private: Ed25519PrivateKey,
    package_version: int,
    min_app_version: str,
    max_app_version: str,
    created_at: str,
    schema_version: int = 1,
) -> Manifest:
    """Stream model/tokenizer artifacts into a signed .pfcmodel (REQ-OCC-03).

    Streams are read from their current position; caller owns all input/output streams.
    """
    return _build(
        output,
        payloads,
        kind="model",
        private=private,
        package_version=package_version,
        schema_version=schema_version,
        min_app_version=min_app_version,
        max_app_version=max_app_version,
        created_at=created_at,
    )


def _check_members(archive: zipfile.ZipFile, limits: PackageLimits) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        raise PackageError(Reason.SIZE_LIMIT, "Too many archive members")
    members: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        _safe_name(info.filename)
        if info.filename in members:
            raise PackageError(Reason.MEMBERS, "Duplicate archive member")
        if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
            raise PackageError(
                Reason.INVALID_ARCHIVE, "Symlinks and encrypted members are prohibited"
            )
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise PackageError(Reason.INVALID_ARCHIVE, "Unsupported ZIP compression")
        if (
            info.file_size > limits.max_member_bytes
            or info.file_size > max(info.compress_size, 1) * limits.max_compression_ratio
        ):
            raise PackageError(Reason.SIZE_LIMIT, "Archive member exceeds size/ratio limit")
        total += info.file_size
        members[info.filename] = info
    if total > limits.max_total_bytes:
        raise PackageError(Reason.SIZE_LIMIT, "Archive exceeds total size limit")
    if MANIFEST_NAME not in members or SIGNATURE_NAME not in members:
        raise PackageError(Reason.MEMBERS, "Missing manifest or signature")
    if members[MANIFEST_NAME].file_size > limits.max_manifest_bytes:
        raise PackageError(Reason.SIZE_LIMIT, "Manifest exceeds size limit")
    if members[SIGNATURE_NAME].file_size != 64:
        raise PackageError(Reason.INVALID_SIGNATURE, "Invalid Ed25519 signature length")
    return members


def verify_package(
    source: bytes | BinaryIO,
    *,
    trusted_keys: Sequence[Ed25519PublicKey],
    app_version: str,
    current_version: int,
    expected_kind: PackageKind = "settings",
    supported_schemas: Sequence[int] = (1,),
    limits: PackageLimits | None = None,
) -> VerifiedPackage:
    """Validate without extracting/applying; reject replay even if signed (REQ-OCC-02/03)."""
    limits = limits or (MODEL_LIMITS if expected_kind == "model" else PackageLimits())
    try:
        with zipfile.ZipFile(
            io.BytesIO(source) if isinstance(source, bytes) else source
        ) as archive:
            members = _check_members(archive, limits)
            encoded = archive.read(MANIFEST_NAME)
            try:
                manifest = Manifest.model_validate(_decode_json(encoded))
                if canonical_json(manifest.model_dump()) != encoded:
                    raise ValueError("Manifest is not canonical")
            except (ValueError, RecursionError) as exc:
                raise PackageError(
                    Reason.INVALID_MANIFEST, "Malformed or noncanonical manifest"
                ) from exc
            public = next((key for key in trusted_keys if key_id(key) == manifest.key_id), None)
            if public is None:
                raise PackageError(Reason.UNTRUSTED_KEY, "Signing key is not trusted")
            verify_signature(encoded, archive.read(SIGNATURE_NAME), public)
            _validate_manifest(manifest)
            if manifest.schema_version not in supported_schemas:
                raise PackageError(Reason.UNSUPPORTED_SCHEMA, "Unsupported package schema")
            if manifest.kind != expected_kind:
                raise PackageError(Reason.WRONG_KIND, "Unexpected package kind")
            try:
                compatible = (
                    Version(manifest.min_app_version)
                    <= Version(app_version)
                    <= Version(manifest.max_app_version)
                )
            except InvalidVersion as exc:
                raise PackageError(Reason.INCOMPATIBLE_APP, "Invalid application version") from exc
            if not compatible:
                raise PackageError(
                    Reason.INCOMPATIBLE_APP, "Application version is outside package range"
                )
            if manifest.package_version <= current_version:
                raise PackageError(
                    Reason.STALE_VERSION, "Package version must be newer than installed"
                )
            if set(members) != {MANIFEST_NAME, SIGNATURE_NAME, *manifest.payloads}:
                raise PackageError(Reason.MEMBERS, "Unexpected or missing archive members")
            settings: dict[str, Any] = {}
            for name, expected in manifest.payloads.items():
                if members[name].file_size != expected.size:
                    raise PackageError(Reason.PAYLOAD_HASH, "Payload size does not match manifest")
                digest, size = hashlib.sha256(), 0
                data = bytearray()
                with archive.open(name) as member:
                    while block := member.read(CHUNK_SIZE):
                        size += len(block)
                        if size > limits.max_member_bytes or size > expected.size:
                            raise PackageError(
                                Reason.SIZE_LIMIT, "Expanded payload exceeds size limit"
                            )
                        digest.update(block)
                        if manifest.kind == "settings":
                            data.extend(block)
                if size != expected.size or digest.hexdigest() != expected.sha256:
                    raise PackageError(
                        Reason.PAYLOAD_HASH, "Payload SHA-256 does not match manifest"
                    )
                if manifest.kind == "settings":
                    try:
                        settings[name] = _decode_json(bytes(data))
                    except (ValueError, RecursionError) as exc:
                        raise PackageError(Reason.INVALID_PAYLOAD, "Invalid settings JSON") from exc
            return VerifiedPackage(manifest, settings, manifest.key_id)
    except PackageError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        OSError,
        EOFError,
        RuntimeError,
        zlib.error,
    ) as exc:
        raise PackageError(Reason.INVALID_ARCHIVE, "Malformed or unreadable ZIP archive") from exc
