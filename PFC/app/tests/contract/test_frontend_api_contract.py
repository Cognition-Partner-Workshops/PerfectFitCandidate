import os
import re
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.main import create_app

Route = tuple[str, str]

API_PREFIX = "/api/v1"
REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND_OVERRIDE = os.environ.get("PFC_FRONTEND_DIR")
if FRONTEND_OVERRIDE and (Path(FRONTEND_OVERRIDE) / "src" / "api").is_dir():
    FRONTEND_ROOT = Path(FRONTEND_OVERRIDE)
else:
    FRONTEND_ROOT = REPO_ROOT / "PFC" / "frontend"

if not FRONTEND_ROOT.is_dir():
    pytest.skip("PFC/frontend is absent", allow_module_level=True)

REQUEST_PATTERN = re.compile(
    r"\bthis\.request(?:\s*<[^>]*>)?\s*\(\s*['\"]"
    r"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['\"]\s*,\s*"
    r"(?P<path>`[^`]*`|'[^']*'|\"[^\"]*\")",
    re.DOTALL,
)
UPLOAD_PATTERN = re.compile(
    r"\bthis\.upload\s*\(\s*(?P<path>`[^`]*`|'[^']*'|\"[^\"]*\")",
    re.DOTALL,
)
ALLOWED_PENDING_LABELS = (
    "W2-1 ingestion",
    "W2-2 matching #10",
    "W2-4b settings package",
    "W2-5 analytics/export",
    "LLM supervisor",
)

PENDING: dict[Route, str] = {
    ("GET", "/api/v1/analytics/bench"): "W2-5 analytics/export",
    ("GET", "/api/v1/analytics/demand"): "W2-5 analytics/export",
    ("GET", "/api/v1/analytics/match"): "W2-5 analytics/export",
    ("GET", "/api/v1/analytics/summary"): "W2-5 analytics/export",
    ("GET", "/api/v1/candidates/{}"): "W2-1 ingestion",
    ("GET", "/api/v1/demands"): "W2-1 ingestion",
    ("GET", "/api/v1/demands/{}/requirements"): "W2-1 ingestion",
    ("GET", "/api/v1/match-runs"): "W2-2 matching #10",
    ("GET", "/api/v1/match-runs/{}/results"): "W2-2 matching #10",
    ("GET", "/api/v1/occ/settings-package"): "W2-4b settings package",
    ("GET", "/api/v1/resources"): "W2-1 ingestion",
    ("GET", "/api/v1/resources/excluded"): "W2-1 ingestion",
    ("GET", "/api/v1/settings/settings-package/history"): "W2-4b settings package",
    ("POST", "/api/v1/demands/analyze"): "W2-1 ingestion",
    ("POST", "/api/v1/demands/{}/requirements/confirm"): "W2-1 ingestion",
    ("POST", "/api/v1/ingestion-runs/{}/commit"): "W2-1 ingestion",
    ("POST", "/api/v1/ingestion-runs/{}/discard"): "W2-1 ingestion",
    ("POST", "/api/v1/match-results/{}/overrides"): "W2-2 matching #10",
    ("POST", "/api/v1/occ/settings-package/export"): "W2-4b settings package",
    ("POST", "/api/v1/pii/unmask"): "W2-2 matching #10",
    ("POST", "/api/v1/resources/cv-sync"): "W2-1 ingestion",
    ("POST", "/api/v1/settings/settings-package/import"): "W2-4b settings package",
    ("POST", "/api/v1/settings/settings-package/preview"): "W2-4b settings package",
    ("POST", "/api/v1/settings/settings-package/rollback"): "W2-4b settings package",
    ("POST", "/api/v1/uploads/{}"): "W2-1 ingestion",
    ("PUT", "/api/v1/candidates/{}/identity"): "W2-1 ingestion",
    ("PUT", "/api/v1/demands/{}/requirements"): "W2-1 ingestion",
    ("PUT", "/api/v1/occ/bench-statuses"): (
        "MISMATCH: merged OCC router only updates /occ/bench-statuses/{code}"
    ),
    ("PUT", "/api/v1/occ/internal-domains"): (
        "MISMATCH: merged OCC router has no collection PUT operation"
    ),
    ("PUT", "/api/v1/occ/resource-team"): (
        "MISMATCH: merged OCC router only updates /occ/resource-team/{target}"
    ),
    ("PUT", "/api/v1/occ/status-map"): (
        "MISMATCH: merged OCC router only updates /occ/status-map/{source}"
    ),
}


def _frontend_path(path_literal: str) -> str:
    path = path_literal[1:-1]
    path = re.sub(r"\$\{this\.qs\([^}]*\)\}", "", path)
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]+\}", "{}", path)
    return f"{API_PREFIX}{path}"


def _is_mock_or_test(source_path: Path, api_directory: Path) -> bool:
    relative_path = source_path.relative_to(api_directory)
    excluded_directories = {"mock", "mocks", "test", "tests", "__tests__"}
    return (
        bool(excluded_directories.intersection(relative_path.parts[:-1]))
        or source_path.name.startswith("test_")
        or source_path.name.endswith((".test.ts", ".spec.ts"))
    )


def _frontend_calls(frontend_root: Path) -> set[Route]:
    api_directory = frontend_root / "src" / "api"
    calls: set[Route] = set()
    for source_path in api_directory.rglob("*.ts"):
        if _is_mock_or_test(source_path, api_directory):
            continue
        source = source_path.read_text(encoding="utf-8")
        calls.update(
            (match.group("method"), _frontend_path(match.group("path")))
            for match in REQUEST_PATTERN.finditer(source)
        )
        calls.update(
            ("POST", _frontend_path(match.group("path")))
            for match in UPLOAD_PATTERN.finditer(source)
        )
    return calls


def _openapi_routes(document: dict[str, object]) -> set[Route]:
    paths = document["paths"]
    assert isinstance(paths, dict)
    routes: set[Route] = set()
    for path, operations in paths.items():
        assert isinstance(path, str)
        assert isinstance(operations, dict)
        normalized_path = re.sub(r"\{[^{}]+\}", "{}", path)
        routes.update(
            (method.upper(), normalized_path)
            for method in operations
            if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
        )
    return routes


def test_frontend_api_calls_match_backend_openapi(tmp_path: Path) -> None:
    frontend_calls = _frontend_calls(FRONTEND_ROOT)
    assert frontend_calls, f"No HTTP calls found under {FRONTEND_ROOT / 'src' / 'api'}"

    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            frontend_dir=tmp_path / "frontend",
        )
    )
    backend_routes = _openapi_routes(app.openapi())

    unlisted_calls = frontend_calls - backend_routes - PENDING.keys()
    assert not unlisted_calls, (
        f"Frontend calls missing from OpenAPI and PENDING: {sorted(unlisted_calls)}"
    )

    pending_now_served = PENDING.keys() & backend_routes
    assert not pending_now_served, (
        f"Remove newly served routes from PENDING: {sorted(pending_now_served)}"
    )

    unused_pending = PENDING.keys() - frontend_calls
    assert not unused_pending, (
        f"PENDING entries no longer called by the frontend: {sorted(unused_pending)}"
    )

    invalid_labels = {
        route: label
        for route, label in PENDING.items()
        if not label.startswith(ALLOWED_PENDING_LABELS) and not label.startswith("MISMATCH")
    }
    assert not invalid_labels, f"Unrecognized PENDING labels: {invalid_labels}"
