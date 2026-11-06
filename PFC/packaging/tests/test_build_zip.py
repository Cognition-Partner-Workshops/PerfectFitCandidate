from __future__ import annotations

import hashlib

import pytest

from PFC.packaging.build_zip import (
    HashMismatchError,
    download_verified,
    parse_manifest,
    parse_models_lock,
    patch_pth_text,
    verify_sha256,
)


def test_patch_pth_enables_site_and_adds_paths() -> None:
    patched = patch_pth_text(
        "python313.zip\r\n.\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n"
    )

    assert "import site" in patched.splitlines()
    assert r"..\app" in patched.splitlines()
    assert r"Lib\site-packages" in patched.splitlines()
    assert patched.splitlines().count(r"..\app") == 1


def test_patch_pth_is_idempotent() -> None:
    content = "python313.zip\n.\nimport site\n..\\app\nLib\\site-packages\n"

    assert patch_pth_text(patch_pth_text(content)) == patch_pth_text(content)


def test_verify_sha256_rejects_mismatch(tmp_path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"pinned content")

    with pytest.raises(HashMismatchError, match="SHA-256 mismatch"):
        verify_sha256(artifact, "0" * 64)


def test_verify_sha256_accepts_matching_digest(tmp_path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"pinned content")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    assert verify_sha256(artifact, digest) == digest


def test_parse_manifest_validates_required_fields() -> None:
    manifest = parse_manifest(
        {
            "tag": "b8936",
            "file": "llama.zip",
            "url": "https://example.invalid/llama.zip",
            "sha256": "a" * 64,
        },
        "tag",
    )

    assert manifest["tag"] == "b8936"
    assert manifest["sha256"] == "a" * 64


def test_parse_manifest_rejects_invalid_hash() -> None:
    with pytest.raises(ValueError, match="64 hexadecimal"):
        parse_manifest(
            {
                "tag": "b8936",
                "file": "llama.zip",
                "url": "https://example.invalid/llama.zip",
                "sha256": "not-a-hash",
            },
            "tag",
        )


def test_parse_models_lock_accepts_null_hash_for_explicit_opt_in() -> None:
    models = parse_models_lock(
        [
            {
                "name": "Model",
                "file": "model.onnx",
                "url": "https://example.invalid/model.onnx",
                "sha256": None,
                "licence": "Apache-2.0",
            }
        ]
    )

    assert models[0]["sha256"] is None


def test_parse_models_lock_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="file must be a filename"):
        parse_models_lock(
            [
                {
                    "name": "Model",
                    "file": r"..\outside.onnx",
                    "url": "https://example.invalid/model.onnx",
                    "sha256": None,
                    "licence": "Apache-2.0",
                }
            ]
        )


def test_download_refuses_unpinned_artifact_by_default(tmp_path) -> None:
    with pytest.raises(ValueError, match="Refusing unpinned download"):
        download_verified("https://example.invalid/model.bin", tmp_path / "model.bin", None)
