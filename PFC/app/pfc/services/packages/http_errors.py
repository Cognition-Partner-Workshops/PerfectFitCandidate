"""Single package error-to-problem mapping for HTTP (REQ-OCC-02/03)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from filelock import Timeout

from pfc.security.package_signing import PackageError
from pfc.security.problems import Problem
from pfc.services.packages.common import PackageServiceError

ERRORS: dict[str, tuple[int, str, str]] = {
    "invalid_archive": (422, "PACKAGE_INVALID_ARCHIVE", "Invalid package archive"),
    "unsafe_path": (422, "PACKAGE_UNSAFE_PATH", "Unsafe archive path"),
    "size_limit": (413, "PACKAGE_TOO_LARGE", "Package exceeds upload or archive limits"),
    "members": (422, "PACKAGE_INVALID_MEMBERS", "Missing or unexpected archive members"),
    "invalid_manifest": (422, "PACKAGE_INVALID_MANIFEST", "Invalid package manifest"),
    "untrusted_key": (422, "PACKAGE_UNTRUSTED_KEY", "Package signing key is not trusted"),
    "invalid_signature": (422, "PACKAGE_INVALID_SIGNATURE", "Invalid package signature"),
    "payload_hash": (422, "PACKAGE_PAYLOAD_HASH", "Package payload hash mismatch"),
    "invalid_payload": (422, "PACKAGE_INVALID_PAYLOAD", "Invalid package payload"),
    "unsupported_schema": (422, "PACKAGE_UNSUPPORTED_SCHEMA", "Unsupported package schema"),
    "incompatible_app": (409, "PACKAGE_INCOMPATIBLE_APP", "Package is incompatible with this app"),
    "stale_version": (409, "PACKAGE_STALE_VERSION", "Package version must be newer than installed"),
    "wrong_kind": (422, "PACKAGE_WRONG_KIND", "Wrong package kind for this operation"),
    "payload_schema": (422, "PACKAGE_PAYLOAD_SCHEMA", "Invalid settings payload schema"),
    "wrong_passphrase": (403, "SIGNING_KEY_PASSPHRASE_INVALID", "Incorrect signing-key passphrase"),
    "passphrase_required": (422, "SIGNING_KEY_PASSPHRASE_REQUIRED", "Signing passphrase required"),
    "public_key_unavailable": (
        404,
        "SIGNING_KEY_UNAVAILABLE",
        "Export first to create a signing key",
    ),
    "dpapi_unavailable": (503, "SIGNING_KEY_DPAPI_UNAVAILABLE", "Windows DPAPI is required"),
    "dpapi_failed": (500, "SIGNING_KEY_DPAPI_FAILED", "Windows DPAPI failed"),
    "invalid_key_file": (500, "SIGNING_KEY_INVALID", "Signing key storage is invalid"),
    "key_mismatch": (500, "SIGNING_KEY_MISMATCH", "Signing key storage does not match"),
    "invalid_trust_store": (500, "PACKAGE_TRUST_STORE_INVALID", "Trust store is invalid"),
    "no_previous_version": (409, "PACKAGE_NO_PREVIOUS_VERSION", "No previous package to restore"),
    "retained_hash": (422, "PACKAGE_RETAINED_HASH", "Retained package integrity check failed"),
    "version_collision": (409, "PACKAGE_VERSION_COLLISION", "Package version already exists"),
    "approval_target_missing": (
        409,
        "PACKAGE_APPROVAL_TARGET_MISSING",
        "Approval target is missing",
    ),
    "secret_provider_url": (
        422,
        "PACKAGE_SECRET_PROVIDER_URL",
        "Provider URL contains credentials",
    ),
    "provider_capabilities": (
        422,
        "PACKAGE_PROVIDER_CAPABILITIES",
        "Invalid provider capabilities",
    ),
    "model_metadata": (422, "PACKAGE_MODEL_METADATA", "Invalid signed model metadata"),
    "model_hash": (422, "PACKAGE_MODEL_HASH", "Model changed during installation"),
    "job_runner_required": (503, "PACKAGE_JOB_RUNNER_REQUIRED", "Job runner is unavailable"),
    "reembedding_pending": (
        503,
        "PACKAGE_REEMBEDDING_PENDING",
        "Model imported; job retry required",
    ),
    "model_installed_pending": (
        409,
        "PACKAGE_MODEL_INSTALLED_PENDING",
        "Model already installed; re-embedding pending. Use POST /models/addons/{version}/reembed",
    ),
    "not_pending": (404, "PACKAGE_NOT_PENDING", "Pending package not found"),
    "inbox_path": (422, "PACKAGE_INBOX_PATH", "Invalid inbox item path"),
    "inbox_changed": (409, "PACKAGE_INBOX_CHANGED", "Inbox package changed; preview again"),
    "preview_missing": (404, "PACKAGE_PREVIEW_NOT_FOUND", "Preview token not found"),
    "preview_expired": (410, "PACKAGE_PREVIEW_EXPIRED", "Preview expired; upload again"),
    "preview_owner": (403, "PACKAGE_PREVIEW_FORBIDDEN", "Preview belongs to a different session"),
    "preview_changed": (409, "PACKAGE_PREVIEW_CHANGED", "Staged upload changed; preview again"),
    "invalid_upload": (400, "PACKAGE_INVALID_UPLOAD", "Upload exactly one multipart file"),
    "upload_media_type": (415, "PACKAGE_UPLOAD_MEDIA_TYPE", "Use multipart/form-data"),
    "busy": (503, "PACKAGE_BUSY", "Package operation is busy; retry later"),
    "not_found": (404, "PACKAGE_NOT_FOUND", "Package file not found"),
    "conflict": (409, "PACKAGE_CONFLICT", "Package conflicts with existing data"),
    "operation_failed": (500, "PACKAGE_OPERATION_FAILED", "Package operation failed"),
}


def problem(reason: str) -> Problem:
    return Problem(*ERRORS.get(reason, ERRORS["operation_failed"]))


@contextmanager
def package_errors() -> Iterator[None]:
    try:
        yield
    except Problem:
        raise
    except (PackageError, PackageServiceError) as exc:
        raise problem(str(exc.reason)) from exc
    except Timeout as exc:
        raise problem("busy") from exc
    except FileNotFoundError as exc:
        raise problem("not_found") from exc
    except sqlite3.IntegrityError as exc:
        raise problem("conflict") from exc
    except Exception as exc:
        raise problem("operation_failed") from exc
