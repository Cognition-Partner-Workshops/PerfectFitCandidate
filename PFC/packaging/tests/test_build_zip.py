from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from PFC.packaging.build_zip import (
    HashMismatchError,
    _safe_path,
    download_verified,
    extract_zip,
    parse_manifest,
    parse_models_lock,
    patch_pth_text,
    unpack_wheels,
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
                "file": "nomic-embed-text-v1.5/tokenizer.json",
                "url": "https://example.invalid/model.onnx",
                "sha256": None,
                "licence": "Apache-2.0",
            }
        ]
    )

    assert models[0]["sha256"] is None
    assert models[0]["file"] == "nomic-embed-text-v1.5/tokenizer.json"


def test_parse_models_lock_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="file must be a safe relative path"):
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


def test_unpack_wheels_maps_package_and_data_files(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "sample_pkg-1.0-py3-none-any.whl"
    files = {
        "sample_pkg/__init__.py": "value = 1\n",
        "sample_pkg-1.0.dist-info/METADATA": "Name: sample-pkg\nVersion: 1.0\n",
        "sample_pkg-1.0.data/purelib/pure_pkg/__init__.py": "value = 2\n",
        "sample_pkg-1.0.data/platlib/platform_module.pyd": "platform binary",
        "sample_pkg-1.0.data/scripts/sample-script.py": "print('script')\n",
        "sample_pkg-1.0.data/data/share/sample.txt": "data file",
        "sample_pkg-1.0.data/headers/include/sample.h": "header",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)

    python_dir = tmp_path / "python"
    unpack_wheels(wheelhouse, python_dir)

    site_packages = python_dir / "Lib" / "site-packages"
    expected_files = {
        site_packages / "sample_pkg" / "__init__.py": files["sample_pkg/__init__.py"],
        site_packages / "sample_pkg-1.0.dist-info" / "METADATA": files[
            "sample_pkg-1.0.dist-info/METADATA"
        ],
        site_packages / "pure_pkg" / "__init__.py": files[
            "sample_pkg-1.0.data/purelib/pure_pkg/__init__.py"
        ],
        site_packages / "platform_module.pyd": files[
            "sample_pkg-1.0.data/platlib/platform_module.pyd"
        ],
        python_dir / "Scripts" / "sample-script.py": files[
            "sample_pkg-1.0.data/scripts/sample-script.py"
        ],
        python_dir / "share" / "sample.txt": files["sample_pkg-1.0.data/data/share/sample.txt"],
        python_dir / "Include" / "include" / "sample.h": files[
            "sample_pkg-1.0.data/headers/include/sample.h"
        ],
    }
    for path, contents in expected_files.items():
        assert path.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize(
    "archive_name",
    ["../outside.txt", r"..\outside.txt", "/absolute.txt", r"C:\outside.txt"],
)
def test_safe_path_rejects_traversal(tmp_path: Path, archive_name: str) -> None:
    with pytest.raises(ValueError, match="Unsafe archive path"):
        _safe_path(tmp_path, archive_name)


def test_extract_zip_creates_explicit_directory_entries(tmp_path: Path) -> None:
    archive_path = tmp_path / "package.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("nested/", "")
        archive.writestr("nested/file.txt", "contents")

    destination = tmp_path / "extracted"
    extract_zip(archive_path, destination)

    assert (destination / "nested").is_dir()
    assert (destination / "nested" / "file.txt").read_text(encoding="utf-8") == "contents"


def test_extract_zip_rejects_traversal_directory_entries(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../evil/", "")

    with pytest.raises(ValueError, match="Unsafe archive path"):
        extract_zip(archive_path, tmp_path / "extracted")

    assert not (tmp_path / "evil").exists()
