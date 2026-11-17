import hashlib
import io
import subprocess

import pytest

from PFC.packaging import build_zip


def test_official_pinned_manifest() -> None:
    data = build_zip.read_json(build_zip.LIBREOFFICE_MANIFEST_PATH)
    manifest = build_zip.parse_manifest(data, "version")
    assert manifest["url"].startswith("https://download.documentfoundation.org/libreoffice/stable/")
    assert manifest["file"] == f"LibreOffice_{manifest['version']}_Win_x86-64.msi"
    assert isinstance(data, dict) and data["licence"] == "MPL-2.0"
    assert data["checksum_url"] == manifest["url"] + ".sha256"


def test_bundle_download_is_hash_verified(tmp_path, monkeypatch) -> None:
    payload = b"MSI"
    calls = []
    data = {
        "version": "25.8.7",
        "file": "lo.msi",
        "url": "https://download.documentfoundation.org/lo.msi",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "licence": "MPL-2.0",
    }
    monkeypatch.setattr(build_zip, "read_json", lambda path: data)
    monkeypatch.setattr(build_zip.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(payload))
    monkeypatch.setattr(build_zip, "extract_libreoffice", lambda *args: calls.append(args))
    assert build_zip.bundle_libreoffice(tmp_path, tmp_path / "output") == "25.8.7"
    assert calls[0][0].read_bytes() == payload
    data["sha256"] = "0" * 64
    with pytest.raises(build_zip.HashMismatchError):
        build_zip.bundle_libreoffice(tmp_path, tmp_path / "output")
    assert len(calls) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_extraction_normalizes_layout_and_retains_licenses(tmp_path, monkeypatch, fallback) -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "msiexec" and fallback:
            return subprocess.CompletedProcess(argv, 1603)
        extracted = tmp_path / "libreoffice-extracted" / "SourceDir" / "LibreOffice"
        (extracted / "program").mkdir(parents=True)
        (extracted / "program" / "soffice.exe").write_bytes(b"executable")
        (extracted / "LICENSE.html").write_text("MPL-2.0")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(build_zip.subprocess, "run", run)
    monkeypatch.setattr(build_zip.shutil, "which", lambda name: "lessmsi")
    destination = tmp_path / "libreoffice"
    build_zip.extract_libreoffice(tmp_path / "lo.msi", destination, tmp_path)
    assert calls[0][:2] == ["msiexec", "/a"]
    assert "TARGETDIR=" + str((tmp_path / "libreoffice-extracted").resolve()) in calls[0]
    assert (destination / "program" / "soffice.exe").read_bytes() == b"executable"
    assert (destination / "LICENSE.html").read_text() == "MPL-2.0"
    if fallback:
        assert calls[1][:2] == ["lessmsi", "x"]


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_layout_is_rejected(tmp_path, count) -> None:
    for index in range(count):
        program = tmp_path / str(index) / "program"
        program.mkdir(parents=True)
        (program / "soffice.exe").touch()
    with pytest.raises(ValueError, match="exactly one"):
        build_zip.normalize_libreoffice_layout(tmp_path, tmp_path / "output")


def test_notices_and_fixture_copy(tmp_path) -> None:
    notice = tmp_path / "THIRD_PARTY_NOTICES.txt"
    build_zip.write_third_party_notices(notice, "3.13.5", "b8936", "25.8.7")
    assert "LibreOffice 25.8.7" in notice.read_text()
    assert "MPL-2.0" in notice.read_text()
    assert "retained in libreoffice/" in notice.read_text()
    fixtures = tmp_path / "smoke-fixtures"
    build_zip.copy_cv_smoke_fixtures(fixtures)
    assert {path.name for path in fixtures.iterdir()} == {"tiny.doc", "tiny.ppt"}
    assert all(path.stat().st_size > 0 for path in fixtures.iterdir())


def test_skip_libreoffice_flag() -> None:
    assert build_zip.build_parser().parse_args(["--skip-libreoffice"]).skip_libreoffice
