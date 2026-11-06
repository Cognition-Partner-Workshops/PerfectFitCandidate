from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

PACKAGE_DIR = Path(__file__).resolve().parent
PFC_DIR = PACKAGE_DIR.parent
REPO_ROOT = PFC_DIR.parent
APP_DIR = PFC_DIR / "app"
FRONTEND_DIR = PFC_DIR / "frontend"
PYTHON_MANIFEST_PATH = PACKAGE_DIR / "python.manifest.json"
LLAMA_MANIFEST_PATH = PACKAGE_DIR / "llama_cpp.manifest.json"
MODELS_LOCK_PATH = PACKAGE_DIR / "MODELS.lock"
REQUIREMENTS_LOCK_PATH = PACKAGE_DIR / "requirements.lock"
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


class HashMismatchError(ValueError):
    pass


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _is_filename(value: str) -> bool:
    filename = PurePosixPath(value.replace("\\", "/"))
    return (
        not filename.is_absolute()
        and len(filename.parts) == 1
        and filename.parts[0] not in {".", ".."}
        and not filename.parts[0].endswith(":")
    )


def _is_safe_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return (
        not path.is_absolute()
        and bool(path.parts)
        and ".." not in path.parts
        and re.match(r"^[A-Za-z]:", path.parts[0]) is None
        and all(part not in {"", "."} for part in normalized.split("/"))
    )


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_manifest(data: object, identity_field: str) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ValueError("Manifest must be a JSON object")

    fields = (identity_field, "file", "url", "sha256")
    values: dict[str, str] = {}
    for field in fields:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Manifest field {field!r} must be a non-empty string")
        values[field] = value.strip()

    if not _is_filename(values["file"]):
        raise ValueError("Manifest file must be a filename")
    if not _is_https_url(values["url"]):
        raise ValueError("Manifest URL must use HTTPS")
    if not SHA256_PATTERN.fullmatch(values["sha256"]):
        raise ValueError("Manifest sha256 must contain exactly 64 hexadecimal characters")
    return values


def parse_models_lock(data: object) -> list[dict[str, str | None]]:
    if not isinstance(data, list):
        raise ValueError("MODELS.lock must contain a JSON array")

    models: list[dict[str, str | None]] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"Model entry {index} must be a JSON object")
        model: dict[str, str | None] = {}
        for field in ("name", "file", "url", "licence"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Model entry {index} field {field!r} is required")
            model[field] = value.strip()

        if not _is_safe_relative_path(model["file"] or ""):
            raise ValueError(f"Model entry {index} file must be a safe relative path")
        model["file"] = (model["file"] or "").replace("\\", "/")
        if not _is_https_url(model["url"] or ""):
            raise ValueError(f"Model entry {index} URL must use HTTPS")

        digest = entry.get("sha256")
        if digest is not None and (
            not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest)
        ):
            raise ValueError(f"Model entry {index} sha256 must be null or a SHA-256 digest")
        model["sha256"] = digest
        models.append(model)
    return models


def patch_pth_text(contents: str) -> str:
    lines = contents.replace("\r\n", "\n").replace("\r", "\n").splitlines()

    site_enabled = False
    for index, line in enumerate(lines):
        if re.fullmatch(r"\s*#\s*import\s+site\s*", line, flags=re.IGNORECASE):
            lines[index] = "import site"
            site_enabled = True
        elif re.fullmatch(r"\s*import\s+site\s*", line, flags=re.IGNORECASE):
            site_enabled = True

    if not site_enabled:
        lines.append("import site")

    existing_paths = {line.strip().casefold() for line in lines}
    for path in (r"..\app", r"Lib\site-packages"):
        if path.casefold() not in existing_paths:
            lines.append(path)
    return "\n".join(lines).rstrip() + "\n"


def patch_python_pth(python_dir: Path) -> None:
    pth_path = python_dir / "python313._pth"
    if not pth_path.is_file():
        raise FileNotFoundError(f"Embedded Python path file not found: {pth_path}")
    contents = pth_path.read_text(encoding="utf-8")
    pth_path.write_text(patch_pth_text(contents), encoding="utf-8", newline="\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected_sha256: str) -> str:
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise ValueError("Expected SHA-256 must contain exactly 64 hexadecimal characters")
    actual = sha256_file(path)
    if actual.lower() != expected_sha256.lower():
        raise HashMismatchError(
            f"SHA-256 mismatch for {path.name}: expected {expected_sha256}, got {actual}"
        )
    return actual


def download_verified(
    url: str,
    destination: Path,
    expected_sha256: str | None,
    allow_unpinned: bool = False,
) -> None:
    if not _is_https_url(url):
        raise ValueError("Download URL must use HTTPS")
    if expected_sha256 is None and not allow_unpinned:
        raise ValueError(f"Refusing unpinned download: {url}")
    if expected_sha256 is not None and not SHA256_PATTERN.fullmatch(expected_sha256):
        raise ValueError("Expected SHA-256 must contain exactly 64 hexadecimal characters")

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial_path = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "PFC packaging"})
        with urllib.request.urlopen(request, timeout=60) as response:
            with partial_path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
        actual = digest.hexdigest()
        if expected_sha256 is None:
            logging.warning("Downloaded unpinned file %s (SHA-256 %s)", url, actual)
        elif actual.lower() != expected_sha256.lower():
            raise HashMismatchError(
                f"SHA-256 mismatch for {destination.name}: expected {expected_sha256}, got {actual}"
            )
        partial_path.replace(destination)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def _safe_path(root: Path, archive_name: str) -> Path:
    normalized_name = archive_name.replace("\\", "/")
    member = PurePosixPath(normalized_name)
    if not _is_safe_relative_path(archive_name):
        raise ValueError(f"Unsafe archive path: {archive_name}")
    return root.joinpath(*member.parts)


def extract_zip(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            target = _safe_path(destination, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def extract_llama_server(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    found_server = False
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            filename = PurePosixPath(info.filename.replace("\\", "/")).name
            if filename.lower() != "llama-server.exe" and not filename.lower().endswith(".dll"):
                continue
            target = _safe_path(destination, filename)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            found_server |= filename.lower() == "llama-server.exe"
    if not found_server:
        raise ValueError("llama.cpp archive does not contain llama-server.exe")


def _wheel_target(python_dir: Path, site_packages: Path, filename: str) -> Path:
    member = PurePosixPath(filename)
    if member.is_absolute() or ".." in member.parts:
        raise ValueError(f"Unsafe wheel path: {filename}")
    parts = member.parts
    if len(parts) >= 3 and parts[0].endswith(".data"):
        category = parts[1]
        relative = PurePosixPath(*parts[2:])
        if category in {"purelib", "platlib"}:
            root = site_packages
        elif category == "scripts":
            root = python_dir / "Scripts"
        elif category == "data":
            root = python_dir
        elif category == "headers":
            root = python_dir / "Include"
        else:
            raise ValueError(f"Unsupported wheel data category: {category}")
        return root.joinpath(*relative.parts)
    return site_packages.joinpath(*parts)


def unpack_wheels(wheelhouse: Path, python_dir: Path) -> None:
    site_packages = python_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise RuntimeError("pip download did not produce any wheels")

    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                target = _wheel_target(python_dir, site_packages, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def run_npm(arguments: list[str]) -> None:
    if sys.platform == "win32":
        command = ["cmd.exe", "/d", "/c", "npm.cmd", *arguments]
    else:
        command = ["npm", *arguments]
    subprocess.run(command, cwd=FRONTEND_DIR, check=True)


def validate_requirements_lock(path: Path) -> None:
    contents = path.read_text(encoding="utf-8")
    if not contents.strip():
        raise ValueError(f"Requirements lock is empty: {path}")
    if "pywin32==" not in contents:
        raise ValueError("Requirements lock must include the Windows pywin32 dependency")
    if not re.search(r"(?m)^    --hash=sha256:[0-9a-f]{64}", contents):
        raise ValueError("Requirements lock must include package SHA-256 hashes")


def load_version() -> str:
    with (APP_DIR / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("PFC/app/pyproject.toml must define a project version")
    return version


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"Required package directory not found: {source}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def build_frontend(web_dir: Path, skip_frontend: bool) -> None:
    dist_dir = FRONTEND_DIR / "dist"
    if not skip_frontend:
        run_npm(["ci"])
        run_npm(["run", "build"])
    if dist_dir.is_dir():
        shutil.copytree(dist_dir, web_dir)
    else:
        web_dir.mkdir(parents=True, exist_ok=True)


def _locked_packages() -> list[str]:
    package_pattern = re.compile(r"^([A-Za-z0-9_.-]+)==([^\\\s]+)")
    packages: list[str] = []
    for line in REQUIREMENTS_LOCK_PATH.read_text(encoding="utf-8").splitlines():
        match = package_pattern.match(line)
        if match:
            packages.append(f"{match.group(1)}=={match.group(2)}")
    return packages


def write_third_party_notices(destination: Path, python_version: str, llama_tag: str) -> None:
    python_packages = _locked_packages()
    model_entries = parse_models_lock(read_json(MODELS_LOCK_PATH))
    lines = [
        "PerfectFitCandidate Windows portable package - third-party notices",
        "",
        f"CPython {python_version} is distributed under the Python Software Foundation License.",
        "llama.cpp is distributed under the MIT License.",
        f"llama.cpp release tag: {llama_tag}",
        "Python dependency license files and metadata are retained under python/Lib/site-packages.",
        "",
        "Vendored Python dependencies:",
        *[f"- {package}" for package in python_packages],
        "",
        "Optional model artifacts:",
        *[f"- {model['name']} ({model['licence']}): {model['url']}" for model in model_entries],
        "",
        "The frontend dependencies are listed in the source repository's "
        "PFC/frontend/package-lock.json.",
    ]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_launcher(destination: Path) -> None:
    launcher = """@echo off
setlocal
set "PFC_HOME=%~dp0"
set "PYTHONHOME=%PFC_HOME%python"
set "PATH=%PYTHONHOME%;%PATH%"
cd /d "%PFC_HOME%"
"%PYTHONHOME%\\python.exe" -m pfc.launcher %*
exit /b %ERRORLEVEL%
"""
    destination.write_text(launcher, encoding="utf-8", newline="\r\n")


def create_zip(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source).as_posix()
            if path.is_dir():
                archive.writestr(f"{relative}/", "")
            else:
                compression = (
                    zipfile.ZIP_STORED
                    if path.suffix.lower() in {".gguf", ".onnx"}
                    else zipfile.ZIP_DEFLATED
                )
                archive.write(path, relative, compress_type=compression)


def build_package(
    output_dir: Path,
    *,
    with_models: bool = False,
    skip_frontend: bool = False,
    allow_unpinned: bool = False,
) -> Path:
    version = load_version()
    output_path = output_dir / f"PFC-{version}-win-x64.zip"
    python_manifest = parse_manifest(read_json(PYTHON_MANIFEST_PATH), "version")
    llama_manifest = parse_manifest(read_json(LLAMA_MANIFEST_PATH), "tag")
    validate_requirements_lock(REQUIREMENTS_LOCK_PATH)

    with tempfile.TemporaryDirectory(prefix="pfc-package-") as temporary_directory:
        workspace = Path(temporary_directory)
        package_root = workspace / "package"
        package_root.mkdir()
        python_archive = workspace / python_manifest["file"]
        download_verified(python_manifest["url"], python_archive, python_manifest["sha256"])
        python_dir = package_root / "python"
        extract_zip(python_archive, python_dir)
        patch_python_pth(python_dir)

        wheelhouse = workspace / "wheelhouse"
        wheelhouse.mkdir()
        pip_command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheelhouse),
            "--platform",
            "win_amd64",
            "--python-version",
            "3.13",
            "--implementation",
            "cp",
            "--abi",
            "cp313",
            "--only-binary=:all:",
            "--require-hashes",
            "--no-deps",
            "--progress-bar",
            "off",
            "-r",
            str(REQUIREMENTS_LOCK_PATH),
        ]
        subprocess.run(pip_command, cwd=REPO_ROOT, check=True)
        unpack_wheels(wheelhouse, python_dir)

        app_dir = package_root / "app"
        app_dir.mkdir()
        _copy_tree(APP_DIR / "pfc", app_dir / "pfc")
        _copy_tree(PFC_DIR / "config", package_root / "config")
        _copy_tree(PFC_DIR / "templates", package_root / "templates")
        build_frontend(package_root / "web", skip_frontend)

        llama_archive = workspace / llama_manifest["file"]
        download_verified(llama_manifest["url"], llama_archive, llama_manifest["sha256"])
        extract_llama_server(llama_archive, package_root / "llama")

        models_dir = package_root / "models"
        models_dir.mkdir()
        if with_models:
            for model in parse_models_lock(read_json(MODELS_LOCK_PATH)):
                model_path = models_dir / (model["file"] or "")
                download_verified(
                    model["url"] or "",
                    model_path,
                    model["sha256"],
                    allow_unpinned=allow_unpinned,
                )

        (package_root / "data").mkdir()
        (package_root / "inbox").mkdir()
        (package_root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        write_launcher(package_root / "PFC.cmd")
        write_third_party_notices(
            package_root / "THIRD_PARTY_NOTICES.txt",
            python_manifest["version"],
            llama_manifest["tag"],
        )
        create_zip(package_root, output_path)

    logging.info("Created %s (%s bytes)", output_path, output_path.stat().st_size)
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the PFC Windows x64 portable ZIP.")
    parser.add_argument(
        "--out",
        type=Path,
        default=PACKAGE_DIR / "dist",
        help="Output directory; the versioned ZIP name is generated automatically.",
    )
    parser.add_argument(
        "--with-models",
        action="store_true",
        help="Include the optional LLM and embedding model files.",
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Skip npm build; copy PFC/frontend/dist if it already exists.",
    )
    parser.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="Permit optional model downloads whose MODELS.lock SHA-256 is null.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        build_package(
            args.out,
            with_models=args.with_models,
            skip_frontend=args.skip_frontend,
            allow_unpinned=args.allow_unpinned,
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        logging.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
